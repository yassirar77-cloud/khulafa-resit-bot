"""Monthly P&L and cash reconciliation from the POS monthly report.

The POS report is not a P&L and two of its headings are actively wrong. This
module applies the two mandatory corrections and nothing else — it never
estimates a missing line.

**(a) "TOTAL PAY SALARY" is not salary.** The block lists suppliers paid by bank
transfer (Balaji, Jasmine Rice, Juta Ria Telur, Mewah Dairies, Reza Plastic).
Booked as printed it understates COGS and overstates wages, which flatters gross
margin and hides food cost. Every row is matched against the supplier master;
a supplier goes to COGS. A name matching neither a supplier nor a staff member
in ``monthly_staff_advance`` is FLAGGED, not guessed — the whole point of the
correction is that the heading cannot be trusted, so an unrecognised name under
it is exactly the case where guessing is most likely to be wrong.

**(b) "PAYOUT PINJAM" is not an expense.** ``ADVANCE TO <staff>`` is money the
business still owns; it is added back. The sum of the ADVANCE TO rows is
asserted against the printed PINJAM total and any gap is flagged.

Everything else is classification of till payouts into COGS, wages and other
expense. Rent, utilities and central payroll are not in the report at all — if
``outlet_config`` has no row for the period the result is labelled CONTRIBUTION
and :func:`format_pl_lines` will not print the words "net profit".

Pure functions; no Supabase or Telegram imports.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from monthly_report_parser import has_paikkasu_tag, strip_paikkasu_tag
from supplier_master import supplier_match_strength

logger = logging.getLogger(__name__)

#: Money is compared to the sen. Anything larger than this is a real gap.
MONEY_TOLERANCE = 0.01

# --- payout classification --------------------------------------------------

#: ``PAY [SALARY] TO KALEEL`` / ``PAY [LP] TO ABU`` / ``PAY [LEAVE PAY] TO HARI``
_BRACKET_TAG_RE = re.compile(r"PAY\s*\[\s*(?P<tag>[^\]]+?)\s*\]", re.IGNORECASE)
#: ``PAY TO AYAM BESTARI`` -> AYAM BESTARI; ``ADVANCE TO YUSUF`` -> YUSUF
_PAYEE_RE = re.compile(r"(?:^|\b)(?:PAY|ADVANCE|ADVANS)\b.*?\bTO\b\s+(?P<payee>.+)$", re.IGNORECASE)

_WAGE_TAGS = frozenset({"SALARY", "LP", "LEAVE PAY", "LEAVEPAY"})

#: Other till expense: keyword -> the bucket shown on the P&L sheet.
_OTHER_EXPENSE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("GAS", "gas"),
    ("HARDWARE", "hardware"),
    ("PEST", "pest_control"),
    ("MEDICAL", "medical"),
    ("KLINIK", "medical"),
    ("CLINIC", "medical"),
    ("LALAMOVE", "delivery"),
    ("GRABEXPRESS", "delivery"),
    ("SHOPEE", "shopee"),
    ("DONATION", "donation"),
    ("DERMA", "donation"),
    ("MASJID", "donation"),
    ("REFUND", "customer_refund"),
    ("TNB", "bills"),
    ("ELECTRIC", "bills"),
    ("LETRIK", "bills"),
    ("AIR SELANGOR", "bills"),
    ("SYABAS", "bills"),
    ("UNIFI", "bills"),
    ("INTERNET", "bills"),
    ("WIFI", "bills"),
    ("BILL", "bills"),
    ("MAINTENANCE", "hardware"),
    ("REPAIR", "hardware"),
    ("SERVIS", "hardware"),
)

#: Till purchases that are food/consumable cost regardless of supplier match.
_COGS_KEYWORDS = ("PAIKKASU", "PASAR", "SAYUR", "AIS", "ICE", "IKAN", "AYAM", "DAGING")

CATEGORY_COGS = "cogs"
CATEGORY_WAGES = "wages"
CATEGORY_OTHER = "other"
CATEGORY_ADVANCE = "advance"
CATEGORY_FLAGGED = "flagged"


def extract_payee(description: Any) -> str:
    """The name after ``TO`` in a payout description, or the whole description.

    ``PAY [SALARY] TO KALEEL`` -> ``KALEEL``; ``PAY TO AYAM BESTARI`` ->
    ``AYAM BESTARI``; ``NON PAY TO CAMELLIA TEA _PAIKKASU_`` -> ``CAMELLIA TEA``.

    The inline ``_PAIKKASU_`` tag is stripped FIRST: left in place it becomes
    part of the payee and every paikkasu row fails supplier-master matching,
    which would push real supplier spend into the unmatched bucket.
    """
    if not isinstance(description, str):
        return ""
    text = strip_paikkasu_tag(description)
    text = _BRACKET_TAG_RE.sub("PAY", text).strip()
    match = _PAYEE_RE.search(text)
    if match:
        return re.sub(r"\s+", " ", match.group("payee")).strip().upper()
    return re.sub(r"\s+", " ", text).strip().upper()


def bracket_tag(description: Any) -> str | None:
    """The ``[SALARY]`` / ``[LP]`` tag, upper-cased, or ``None``."""
    if not isinstance(description, str):
        return None
    match = _BRACKET_TAG_RE.search(description)
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group("tag")).strip().upper()


def _matches_staff(payee: str, staff_names: set[str]) -> bool:
    """True when the payee is a known staff member from monthly_staff_advance."""
    if not payee:
        return False
    if payee in staff_names:
        return True
    # Staff are printed inconsistently ("B ALAM" vs "ALAM"); accept a whole-word
    # containment either way, but never a bare substring (which would match
    # "ALI" inside "BALAJI").
    for name in staff_names:
        if not name:
            continue
        if re.search(rf"\b{re.escape(name)}\b", payee) or re.search(rf"\b{re.escape(payee)}\b", name):
            return True
    return False


def classify_payout(description: Any, *, block: str, staff_names: set[str] | None = None) -> dict:
    """Classify one payout row into a P&L category.

    Returns ``{"category", "bucket", "payee", "reason"}`` where ``category`` is
    one of cogs / wages / other / advance / flagged.

    The ``salary_block`` (correction (a)) is the delicate one: a supplier there
    is COGS, a staff member there is wages, and anything else is FLAGGED with a
    reason rather than assigned. ``block='purchase'`` rows tagged ``[SALARY]`` or
    ``[LP]`` are genuine till wages — that tag is printed by the cashier at the
    moment of payment and has proven reliable, unlike the block heading.
    """
    staff_names = staff_names or set()
    payee = extract_payee(description)
    tag = bracket_tag(description)
    upper = re.sub(r"\s+", " ", str(description or "")).strip().upper()

    if block == "pinjam" or upper.startswith("ADVANCE TO") or upper.startswith("ADVANS TO"):
        return {"category": CATEGORY_ADVANCE, "bucket": "staff_advance",
                "payee": payee, "reason": "recoverable staff advance — added back"}

    if block == "salary_block":
        strength = supplier_match_strength(payee)
        if strength == "strong":
            return {"category": CATEGORY_COGS, "bucket": "supplier_bank",
                    "payee": payee, "reason": "matched supplier master"}
        if _matches_staff(payee, staff_names):
            return {"category": CATEGORY_WAGES, "bucket": "salary_bank",
                    "payee": payee, "reason": "matched staff in monthly_staff_advance"}
        if strength == "ambiguous":
            return {"category": CATEGORY_FLAGGED, "bucket": "unresolved",
                    "payee": payee,
                    "reason": "supplier match is ambiguous (short token) and no staff match"}
        return {"category": CATEGORY_FLAGGED, "bucket": "unresolved", "payee": payee,
                "reason": "matches neither the supplier master nor a staff name"}

    # --- till purchases block ---
    # Paikkasu is an inline TAG, not a section and not a prefix: it appears on
    # both "PAY TO …" and "NON PAY TO …" rows. Classify on the tag before
    # anything else, so a paikkasu row can never be mistaken for an expense by
    # a keyword that happens to appear in the supplier's name.
    if has_paikkasu_tag(description):
        return {"category": CATEGORY_COGS, "bucket": "paikkasu", "payee": payee,
                "reason": "inline _PAIKKASU_ tag"}

    if tag in _WAGE_TAGS:
        return {"category": CATEGORY_WAGES, "bucket": "till_wages", "payee": payee,
                "reason": f"payout tagged [{tag}]"}

    for keyword, bucket in _OTHER_EXPENSE_KEYWORDS:
        if keyword in upper:
            return {"category": CATEGORY_OTHER, "bucket": bucket, "payee": payee,
                    "reason": f"matched other-expense keyword {keyword!r}"}

    if any(k in upper for k in _COGS_KEYWORDS) or supplier_match_strength(payee) == "strong":
        return {"category": CATEGORY_COGS, "bucket": "till_purchase", "payee": payee,
                "reason": "supplier / food keyword"}

    # An untagged, unrecognised till payout is still a purchase by default —
    # the block IS the purchase block, and its heading (unlike the salary one)
    # has not been observed to lie. Recorded as low-confidence so the sheet can
    # show what went to COGS on that basis alone.
    return {"category": CATEGORY_COGS, "bucket": "till_purchase_unmatched", "payee": payee,
            "reason": "in the purchase block with no supplier/keyword match"}


def classify_payouts(payouts: list[dict], staff_names: set[str] | None = None) -> list[dict]:
    """Classify every payout row, returning copies with the verdict attached."""
    out = []
    for row in payouts or []:
        if not isinstance(row, dict):
            continue
        verdict = classify_payout(
            row.get("description"), block=row.get("block") or "purchase",
            staff_names=staff_names,
        )
        out.append({**row, **verdict})
    return out


# --- the two mandatory assertions -------------------------------------------


def check_pinjam_addback(pinjam_rows: list[dict], printed_total: Any) -> dict:
    """Correction (b): Σ ``ADVANCE TO *`` must equal the printed PINJAM total.

    Returns ``{"addback", "printed", "delta", "ok", "flag"}``. ``addback`` is the
    summed detail — the figure actually added back — because the detail is what
    the staff-advance ledger is built from. A mismatch does not stop the close;
    it is reported, since either number being wrong is a finding.
    """
    rows = [r for r in (pinjam_rows or []) if isinstance(r, dict)]
    addback = round(sum(float(r.get("amount") or 0) for r in rows), 2)
    printed = None
    if isinstance(printed_total, (int, float)) and not isinstance(printed_total, bool):
        printed = round(float(printed_total), 2)
    if printed is None:
        return {"addback": addback, "printed": None, "delta": None, "ok": False,
                "flag": "no printed PINJAM total to check the detail against"}
    delta = round(addback - printed, 2)
    ok = abs(delta) <= MONEY_TOLERANCE
    return {
        "addback": addback, "printed": printed, "delta": delta, "ok": ok,
        "flag": None if ok else (
            f"ADVANCE TO rows sum to RM{addback:.2f} but PAYOUT PINJAM prints "
            f"RM{printed:.2f} (delta RM{delta:+.2f})"
        ),
    }


def reclassify_salary_block(salary_rows: list[dict], staff_names: set[str] | None = None) -> dict:
    """Correction (a): split the mislabelled SALARY block into COGS / wages / flagged.

    Returns totals plus the flagged rows themselves, so the digest can name the
    payees a human has to resolve instead of just counting them.
    """
    classified = classify_payouts(
        [{**r, "block": "salary_block"} for r in (salary_rows or [])], staff_names
    )
    cogs = round(sum(float(r.get("amount") or 0) for r in classified
                     if r["category"] == CATEGORY_COGS), 2)
    wages = round(sum(float(r.get("amount") or 0) for r in classified
                      if r["category"] == CATEGORY_WAGES), 2)
    flagged_rows = [r for r in classified if r["category"] == CATEGORY_FLAGGED]
    flagged = round(sum(float(r.get("amount") or 0) for r in flagged_rows), 2)
    if flagged_rows:
        logger.warning(
            "monthly close: %d row(s) in the SALARY block match neither a "
            "supplier nor a staff name (RM%.2f): %s",
            len(flagged_rows), flagged, [r.get("payee") for r in flagged_rows][:10],
        )
    return {
        "supplier_cogs": cogs, "staff_wages": wages,
        "flagged_amount": flagged, "flagged_rows": flagged_rows,
        "rows": classified,
    }


# --- P&L --------------------------------------------------------------------


def _sum(rows: list[dict], predicate) -> float:
    return round(sum(float(r.get("amount") or 0) for r in rows if predicate(r)), 2)


def build_pl(
    totals: dict,
    payouts: list[dict],
    pinjam_rows: list[dict],
    salary_rows: list[dict],
    staff_names: set[str] | None = None,
    outlet_config: dict | None = None,
) -> dict:
    """Build the month's P&L.

    ``payouts`` are the till purchase-block rows; ``salary_rows`` the mislabelled
    bank block; ``pinjam_rows`` the advances. ``outlet_config`` supplies the
    bank-paid lines — when it is ``None``/empty the result stops at CONTRIBUTION
    and ``net_profit`` is ``None``. It is never estimated.
    """
    totals = totals or {}
    staff_names = staff_names or set()

    till = classify_payouts(
        [{**r, "block": "purchase"} for r in (payouts or [])], staff_names
    )
    salary = reclassify_salary_block(salary_rows, staff_names)
    pinjam = check_pinjam_addback(pinjam_rows, totals.get("payout_pinjam")
                                  if totals.get("payout_pinjam") is not None
                                  else totals.get("total_pinjam"))

    sales = float(totals.get("total_sales") or 0)

    # Paikkasu has NO section and NO printed total — it is an inline tag on rows
    # that already live in the payouts block. So it is not added to COGS; it is
    # SPLIT OUT of the till COGS rows for reporting. Adding a separate paikkasu
    # figure on top would double-count every tagged row.
    till_purchases = _sum(
        till, lambda r: r["category"] == CATEGORY_COGS and r["bucket"] != "paikkasu"
    )
    paikkasu = _sum(till, lambda r: r["bucket"] == "paikkasu")

    supplier_bank = salary["supplier_cogs"]
    cogs = round(till_purchases + paikkasu + supplier_bank, 2)
    gross_profit = round(sales - cogs, 2)

    till_wages = round(_sum(till, lambda r: r["category"] == CATEGORY_WAGES)
                       + salary["staff_wages"], 2)
    other_till = _sum(till, lambda r: r["category"] == CATEGORY_OTHER)
    other_breakdown: dict[str, float] = {}
    for row in till:
        if row["category"] != CATEGORY_OTHER:
            continue
        other_breakdown[row["bucket"]] = round(
            other_breakdown.get(row["bucket"], 0.0) + float(row.get("amount") or 0), 2
        )

    contribution = round(gross_profit - till_wages - other_till, 2)

    config = outlet_config or {}
    config_fields = ("rent", "tnb", "air_selangor", "unifi", "central_payroll", "kwsp_perkeso")
    config_values = {
        f: (float(config[f]) if isinstance(config.get(f), (int, float))
            and not isinstance(config.get(f), bool) else None)
        for f in config_fields
    }
    has_config = any(v is not None for v in config_values.values())
    fixed_costs = round(sum(v for v in config_values.values() if v is not None), 2) if has_config else None
    net_profit = round(contribution - fixed_costs, 2) if has_config else None

    flags: list[str] = []
    if pinjam.get("flag"):
        flags.append(pinjam["flag"])
    if salary["flagged_rows"]:
        names = ", ".join(str(r.get("payee")) for r in salary["flagged_rows"][:5])
        flags.append(
            f"{len(salary['flagged_rows'])} bank-block row(s) worth "
            f"RM{salary['flagged_amount']:.2f} match neither a supplier nor a "
            f"staff name — unclassified, NOT in COGS or wages ({names})"
        )
    if not has_config:
        flags.append(
            "outlet_config has no row for this period — rent, utilities and "
            "central payroll are unknown, so this is CONTRIBUTION, not net profit"
        )
    if sales <= 0:
        flags.append("TOTAL SALES is zero or missing — check the source report")

    return {
        "sales": round(sales, 2),
        "cogs": cogs,
        "cogs_pct": round(cogs / sales * 100, 1) if sales else None,
        "cogs_breakdown": {
            "till_purchases": till_purchases,
            "paikkasu": paikkasu,
            "supplier_bank_block": supplier_bank,
        },
        "gross_profit": gross_profit,
        "gross_margin_pct": round(gross_profit / sales * 100, 1) if sales else None,
        "till_wages": till_wages,
        "other_till_expense": other_till,
        "other_breakdown": other_breakdown,
        "contribution": contribution,
        "pinjam_addback": pinjam["addback"],
        "pinjam_check": pinjam,
        "salary_reclass": salary,
        "outlet_config": config_values if has_config else None,
        "fixed_costs": fixed_costs,
        "net_profit": net_profit,
        "is_contribution_only": not has_config,
        "label": (
            "NET PROFIT" if has_config
            else "CONTRIBUTION (pre-rent/utilities/central payroll)"
        ),
        "flags": flags,
        "classified_payouts": till + salary["rows"],
    }


# --- cash reconciliation ----------------------------------------------------


def build_cash_reconciliation(totals: dict) -> dict:
    """Cash that should have reached the bank.

    ``cash_sales = TOTAL SALES − TOTAL BANK SALES − TOTAL FP SALES``
    ``expected_bank_in = cash_sales − TOTAL PAYOUTS``

    ``expected_bank_in`` is the figure to match against the bank statement. A
    negative value is flagged: it means the till paid out more cash than it took,
    which is possible over a month but never normal.
    """
    totals = totals or {}

    def value(key):
        raw = totals.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw)

    sales = value("total_sales")
    bank = value("total_bank_sales")
    fp = value("total_fp_sales")
    payouts = value("total_payouts")

    missing = [k for k, v in (
        ("TOTAL SALES", sales), ("TOTAL BANK SALES", bank),
        ("TOTAL FP SALES", fp), ("TOTAL PAYOUTS", payouts),
    ) if v is None]

    if sales is None:
        return {"cash_sales": None, "expected_bank_in": None, "missing": missing,
                "flags": ["TOTAL SALES missing — cannot reconcile cash"],
                "components": {"total_sales": sales, "total_bank_sales": bank,
                               "total_fp_sales": fp, "total_payouts": payouts}}

    cash_sales = round(sales - (bank or 0) - (fp or 0), 2)
    expected_bank_in = round(cash_sales - (payouts or 0), 2)

    flags: list[str] = []
    if missing:
        flags.append(
            "treated as zero because the report did not print them: "
            + ", ".join(missing)
        )
    if expected_bank_in < 0:
        flags.append(
            f"expected_bank_in is NEGATIVE (RM{expected_bank_in:.2f}) — cash "
            "payouts exceeded cash sales for the month; check for a payout "
            "keyed against the wrong outlet"
        )
    if cash_sales < 0:
        flags.append(
            f"cash_sales is NEGATIVE (RM{cash_sales:.2f}) — bank + FP sales "
            "exceed total sales, which means a channel total is wrong"
        )

    return {
        "cash_sales": cash_sales,
        "expected_bank_in": expected_bank_in,
        "missing": missing,
        "flags": flags,
        "components": {
            "total_sales": sales, "total_bank_sales": bank,
            "total_fp_sales": fp, "total_payouts": payouts,
        },
    }


# --- presentation -----------------------------------------------------------


def format_monthly_digest(
    outlet: str, period: str, pl: dict, cash: dict,
    wastage: dict | None = None, hygiene: dict | None = None,
) -> str:
    """The Telegram digest posted to the outlet group.

    Deliberately leads with the two numbers a manager can act on — COGS % and
    expected bank-in — and never prints a bottom line called "net profit" unless
    the fixed costs were actually supplied. Flag counts are shown even when zero
    is the happy answer, because a silent report and a clean report must not look
    the same.
    """
    from monthly_wastage import top_variances

    lines = [
        f"📊 Monthly close — {outlet}, {period}",
        "",
        f"Sales:          RM{pl['sales']:,.2f}",
        f"COGS:           RM{pl['cogs']:,.2f}"
        + (f"  ({pl['cogs_pct']:.1f}%)" if pl["cogs_pct"] is not None else ""),
        f"Gross profit:   RM{pl['gross_profit']:,.2f}",
        f"Till wages:     RM{pl['till_wages']:,.2f}",
        f"Other expense:  RM{pl['other_till_expense']:,.2f}",
    ]
    if pl["is_contribution_only"]:
        lines.append(f"CONTRIBUTION:   RM{pl['contribution']:,.2f}")
        lines.append("  (pre-rent/utilities/central payroll — NOT net profit)")
    else:
        lines.append(f"Contribution:   RM{pl['contribution']:,.2f}")
        lines.append(f"NET PROFIT:     RM{pl['net_profit']:,.2f}")

    expected = cash.get("expected_bank_in")
    lines += ["", "💵 Cash"]
    if expected is None:
        lines.append("Expected bank-in: — (report missing TOTAL SALES)")
    else:
        lines.append(f"Cash sales:       RM{cash['cash_sales']:,.2f}")
        lines.append(f"Expected bank-in: RM{expected:,.2f}")
        if expected < 0:
            lines.append("⚠️ NEGATIVE — payouts exceeded cash sales")

    if wastage:
        top = top_variances(wastage, 3)
        lines += ["", "🥘 Top wastage variances"]
        if not top:
            lines.append("(none computable — see the flagged rows below)")
        for row in top:
            lines.append(
                f"• {row['canonical_item']}: {row['variance_pct']:+.1f}%  "
                f"{row['verdict']}"
            )

    flag_count = len(pl.get("flags") or []) + len(cash.get("flags") or [])
    unreliable = (wastage or {}).get("unreliable_count", 0)
    flagged_rows = (wastage or {}).get("flagged_rows", 0)
    lines += ["", "🚩 Flags"]
    lines.append(f"• {flag_count} P&L / cash flag(s)")
    lines.append(f"• {unreliable} ingredient(s) with no reliable variance")
    lines.append(f"• {flagged_rows} purchase line(s) not convertible to a base unit")
    if hygiene:
        lines.append(
            f"• {hygiene.get('duplicate_count', 0)} duplicate SKU group(s), "
            f"{hygiene.get('dead_count', 0)} dead SKU(s)"
        )
    for flag in (pl.get("flags") or [])[:3]:
        lines.append(f"  – {flag}")
    return "\n".join(lines)


def format_pl_lines(pl: dict) -> list[tuple[str, Any]]:
    """The P&L as ordered ``(label, value)`` rows for the sheet and the digest.

    When ``outlet_config`` is absent the final line reads CONTRIBUTION and the
    words "net profit" do not appear anywhere in the output.
    """
    rows: list[tuple[str, Any]] = [
        ("Sales", pl["sales"]),
        ("COGS — till purchases", pl["cogs_breakdown"]["till_purchases"]),
        ("COGS — paikkasu", pl["cogs_breakdown"]["paikkasu"]),
        ("COGS — supplier bank block (reclassified from 'SALARY')",
         pl["cogs_breakdown"]["supplier_bank_block"]),
        ("COGS total", pl["cogs"]),
        ("COGS %", pl["cogs_pct"]),
        ("Gross profit", pl["gross_profit"]),
        ("Till wages (PAY [SALARY] + PAY [LP])", pl["till_wages"]),
        ("Other till expense", pl["other_till_expense"]),
    ]
    for bucket, amount in sorted(pl["other_breakdown"].items()):
        rows.append((f"    · {bucket.replace('_', ' ')}", amount))
    rows.append(("Staff advance added back (PAYOUT PINJAM)", pl["pinjam_addback"]))
    rows.append(("OUTLET CONTRIBUTION", pl["contribution"]))
    if pl["is_contribution_only"]:
        rows.append((pl["label"], pl["contribution"]))
    else:
        config = pl["outlet_config"] or {}
        for field in ("rent", "tnb", "air_selangor", "unifi", "central_payroll", "kwsp_perkeso"):
            rows.append((f"    − {field.replace('_', ' ')}", config.get(field)))
        rows.append(("NET PROFIT", pl["net_profit"]))
    return rows
