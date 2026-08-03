#!/usr/bin/env python3
"""SHADOW-RUN the invoice extractor to build the uom_conversion seeding worklist.

`uom_conversion` ships with only the factors that are definitions — the KG/G and
L/ML families and the count words. Every other printed unit (GUNI, PAPAN, CTN,
BTL, PKT, ...) is deliberately absent, because a sack of beras is 25kg at one
supplier and 10kg at another and guessing would silently corrupt every wastage
number computed from it.

That leaves a question: which factors are worth measuring FIRST? This script
answers it with money. It re-reads the last N days of SUPPLIER_PURCHASE
receipts through the SAME line-items OCR pass production uses, resolves each
line against the live `uom_conversion` table, and ranks every
(supplier, canonical_item, unit_raw) combination that found NO match by the
ringgit flowing through it.

READ-ONLY. It never writes `receipt_items`, never writes `uom_conversion`, and
never touches `receipts`. The only mutation it can perform is writing the CSV
file you ask for.

Output columns (sorted by total_RM desc):

    supplier, canonical_item, unit_raw, line_count, total_RM

Usage::

    SUPABASE_URL=... SUPABASE_KEY=... ZAI_API_KEY=... \\
    python scripts/uom_seeding_worklist.py --days 60 --out worklist.csv

    # Pick the receipts and report the cost, without spending an OCR call:
    python scripts/uom_seeding_worklist.py --days 60 --dry-run

COST: one vision call per receipt. `--dry-run` tells you how many that will be
before you spend anything; `--limit` and `--max-calls` bound it. Receipts are
processed one at a time — same reasoning as OCR_MAX_CONCURRENCY=1.
"""
from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from invoice_ocr import call_line_items_ocr  # noqa: E402
from item_canonicalization_v2 import canonicalize_item  # noqa: E402
from receipt_validation import check_subtotal  # noqa: E402
from uom_conversion import UOM_CONVERSION_TABLE, resolve_conversion  # noqa: E402
from wastage_export import line_cost  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("uom_seeding_worklist")

RECEIPTS_TABLE = "receipts"
SUPPLIER_PURCHASE = "SUPPLIER_PURCHASE"
SELECT_COLUMNS = (
    "id, merchant, outlet, receipt_date, created_at, total, receipt_type, "
    "image_url, photo_file_id"
)

CSV_COLUMNS = ("supplier", "canonical_item", "unit_raw", "line_count", "total_RM")

#: Reported when the canonicaliser did not recognise the item — the worklist
#: row is still actionable, but only as a supplier-wide or unit-wide factor,
#: never as an item-scoped one.
UNCATEGORISED = "UNCATEGORISED"

DEFAULT_DAYS = 60
DEFAULT_LIMIT = 400
IMAGE_MAX_DIM = int(os.environ.get("IMAGE_MAX_DIM", "1600"))
IMAGE_TIMEOUT = float(os.environ.get("IMAGE_FETCH_TIMEOUT", "30"))


# --- data access -----------------------------------------------------------


def build_client():
    from supabase import create_client

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def _today_my() -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date()


def fetch_supplier_receipts(
    client, *, days: int, date_field: str, limit: int
) -> list[dict]:
    """Supplier purchases in the window, newest first.

    ``date_field`` is ``receipt_date`` (the invoice's own date — what you want
    when asking "what are we buying lately") or ``created_at`` (upload time —
    what you want when the invoice dates are unreliable).
    """
    from db_pagination import fetch_all_pages

    cutoff = _today_my() - timedelta(days=days)
    cutoff_value = (
        cutoff.isoformat()
        if date_field == "receipt_date"
        else datetime.combine(cutoff, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    )

    def make_query():
        return (
            client.table(RECEIPTS_TABLE)
            .select(SELECT_COLUMNS)
            .eq("receipt_type", SUPPLIER_PURCHASE)
            .gte(date_field, cutoff_value)
            .order("id", desc=True)
        )

    rows = fetch_all_pages(make_query)
    return rows[:limit] if limit else rows


def load_conversions_strict(client) -> list[dict]:
    """Read ``uom_conversion``, distinguishing "empty" from "unreadable".

    ``uom_conversion.load_conversions`` degrades to ``[]`` on error, which is
    right for the live pipeline (a missing table must not stop a receipt being
    saved) and wrong here: it would report KG as an unseeded unit and produce a
    worklist telling you to measure a kilogram. Raises instead.
    """
    result = client.table(UOM_CONVERSION_TABLE).select("*").execute()
    return list(getattr(result, "data", None) or [])


# --- image recovery --------------------------------------------------------


def fetch_image_bytes(row: dict, *, telegram_token: str | None = None) -> bytes | None:
    """Recover a receipt's photo: Cloudinary archive first, Telegram second.

    Returns ``None`` when neither source has it (receipts predating migration
    0027 have no image at all, and Telegram file handles expire).
    """
    import httpx

    url = row.get("image_url")
    if url:
        try:
            response = httpx.get(url, timeout=IMAGE_TIMEOUT, follow_redirects=True)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001 - fall through to Telegram
            logger.warning("receipt %s: image_url fetch failed (%s)", row.get("id"), exc)

    file_id = row.get("photo_file_id")
    if file_id and telegram_token:
        try:
            meta = httpx.get(
                f"https://api.telegram.org/bot{telegram_token}/getFile",
                params={"file_id": file_id},
                timeout=IMAGE_TIMEOUT,
            )
            meta.raise_for_status()
            path = meta.json()["result"]["file_path"]
            blob = httpx.get(
                f"https://api.telegram.org/file/bot{telegram_token}/{path}",
                timeout=IMAGE_TIMEOUT,
            )
            blob.raise_for_status()
            return blob.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("receipt %s: telegram fetch failed (%s)", row.get("id"), exc)

    return None


def prepare_image(image_bytes: bytes) -> bytes:
    """Downscale exactly as the live pipeline does before the OCR call."""
    try:
        from image_utils import resize_for_ocr

        return resize_for_ocr(image_bytes, IMAGE_MAX_DIM)
    except Exception:  # noqa: BLE001 - Pillow missing / undecodable: send as-is
        logger.warning("resize unavailable; sending original bytes", exc_info=True)
        return image_bytes


# --- the shadow run --------------------------------------------------------


def collect_misses(
    receipt: dict, parsed_invoice: dict, conversions: list[dict]
) -> tuple[list[dict], dict]:
    """Find the unconvertible lines of one invoice.

    Returns ``(misses, stats)``. Each miss is
    ``{supplier, canonical_item, unit_raw, cost}``.

    Two deliberate choices:

    * The miss test is :func:`uom_conversion.resolve_conversion`, NOT
      ``convert_to_base`` — a line whose quantity was unreadable still names a
      unit that needs a factor, and conflating "no factor" with "no quantity"
      would put phantom work on the list.
    * A receipt that fails the subtotal check still contributes. The live
      pipeline withholds its quantities, but the units printed on it are real
      and still need seeding; the failure is counted in ``stats`` instead.
    """
    supplier = receipt.get("merchant") or parsed_invoice.get("supplier")
    lines = parsed_invoice.get("lines") or []
    subtotal = check_subtotal(lines, parsed_invoice.get("subtotal"))

    misses: list[dict] = []
    no_unit = 0
    for line in lines:
        if not isinstance(line, dict):
            continue
        item_text = line.get("item")
        if not isinstance(item_text, str) or not item_text.strip():
            continue
        unit_raw = line.get("unit")
        unit_raw = unit_raw.strip() if isinstance(unit_raw, str) else ""
        if not unit_raw:
            # Nothing to seed: no unit was printed. Counted, not listed.
            no_unit += 1
            continue
        canonical = canonicalize_item(item_text).get("canonical")
        if resolve_conversion(conversions, supplier, canonical, unit_raw) is not None:
            continue
        misses.append({
            "supplier": (supplier or "").strip() or "UNKNOWN",
            "canonical_item": canonical or UNCATEGORISED,
            "unit_raw": unit_raw.upper(),
            "cost": line_cost(line) or 0.0,
        })

    stats = {
        "lines": len(lines),
        "no_unit": no_unit,
        "misses": len(misses),
        "subtotal_mismatch": bool(subtotal.get("needs_review")),
    }
    return misses, stats


def rank_worklist(misses: list[dict]) -> list[dict]:
    """Group misses by (supplier, canonical_item, unit_raw), rank by money.

    Ties break on line_count then alphabetically, so the same input always
    produces byte-identical CSV — a worklist that reshuffles between runs is
    one nobody can diff.
    """
    grouped: dict[tuple[str, str, str], dict] = {}
    for miss in misses:
        key = (miss["supplier"], miss["canonical_item"], miss["unit_raw"])
        bucket = grouped.setdefault(key, {"line_count": 0, "total_RM": 0.0})
        bucket["line_count"] += 1
        bucket["total_RM"] += miss.get("cost") or 0.0

    rows = [
        {
            "supplier": supplier,
            "canonical_item": item,
            "unit_raw": unit,
            "line_count": bucket["line_count"],
            "total_RM": round(bucket["total_RM"], 2),
        }
        for (supplier, item, unit), bucket in grouped.items()
    ]
    rows.sort(key=lambda r: (-r["total_RM"], -r["line_count"],
                             r["supplier"], r["canonical_item"], r["unit_raw"]))
    return rows


def build_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([
            row["supplier"], row["canonical_item"], row["unit_raw"],
            row["line_count"], f"{row['total_RM']:.2f}",
        ])
    return buffer.getvalue()


def format_report(rows: list[dict], stats: dict) -> str:
    """Human summary for the console — what was covered and what was skipped."""
    total_rm = round(sum(r["total_RM"] for r in rows), 2)
    out = [
        "",
        "=== uom_conversion seeding worklist ===",
        f"Window:            last {stats['days']} days by {stats['date_field']}",
        f"Supplier receipts: {stats['receipts_selected']} selected"
        f" · {stats['receipts_ocred']} OCR'd"
        f" · {stats['receipts_no_image']} with no recoverable image"
        f" · {stats['receipts_ocr_failed']} OCR failures",
        f"Lines read:        {stats['lines']}"
        f" · {stats['no_unit']} printed no unit (nothing to seed)"
        f" · {stats['misses']} unconvertible",
        f"Unseeded combos:   {len(rows)} worth RM{total_rm:.2f}",
    ]
    if stats.get("subtotal_mismatch"):
        out.append(
            f"Note:              {stats['subtotal_mismatch']} invoice(s) failed the "
            "subtotal check — their units are still listed (the printed unit is "
            "real even when the arithmetic isn't)."
        )
    if stats.get("truncated"):
        out.append(
            f"⚠️  Capped at {stats['truncated']} receipts (--limit/--max-calls); "
            "the worklist is a lower bound, not the full picture."
        )
    if rows:
        out += ["", "Top 10 by RM:"]
        width = max(len(r["supplier"]) for r in rows[:10])
        for row in rows[:10]:
            out.append(
                f"  RM{row['total_RM']:>10,.2f}  {row['supplier']:<{width}}  "
                f"{row['canonical_item']:<16} {row['unit_raw']:<8} "
                f"×{row['line_count']}"
            )
    else:
        out.append("Nothing unconvertible in the window — no seeding needed.")
    return "\n".join(out)


def run(
    client,
    *,
    days: int = DEFAULT_DAYS,
    date_field: str = "receipt_date",
    limit: int = DEFAULT_LIMIT,
    max_calls: int | None = None,
    dry_run: bool = False,
    ocr_call=None,
    image_fetcher=None,
    allow_empty_conversions: bool = False,
) -> tuple[list[dict], dict]:
    """Execute the shadow run. Returns ``(worklist_rows, stats)``.

    ``ocr_call(image_bytes) -> parsed_invoice`` and
    ``image_fetcher(receipt_row) -> bytes | None`` are injectable so the whole
    path is testable without a network.
    """
    conversions = load_conversions_strict(client)
    if not conversions and not allow_empty_conversions:
        raise SystemExit(
            "uom_conversion is empty — apply migrations/0038_receipt_items_uom.sql "
            "first, or pass --allow-empty-conversions if you really mean to rank "
            "every unit including KG."
        )
    logger.info("Loaded %d uom_conversion row(s)", len(conversions))

    receipts = fetch_supplier_receipts(
        client, days=days, date_field=date_field, limit=limit
    )
    stats = {
        "days": days,
        "date_field": date_field,
        "receipts_selected": len(receipts),
        "receipts_ocred": 0,
        "receipts_no_image": 0,
        "receipts_ocr_failed": 0,
        "lines": 0,
        "no_unit": 0,
        "misses": 0,
        "subtotal_mismatch": 0,
        "truncated": 0,
    }

    if dry_run:
        logger.info(
            "DRY RUN: %d supplier receipt(s) in the window; a real run would "
            "make up to that many OCR calls.", len(receipts),
        )
        return [], stats

    budget = max_calls if max_calls is not None else len(receipts)
    if budget < len(receipts):
        stats["truncated"] = budget
        receipts = receipts[:budget]

    all_misses: list[dict] = []
    for index, receipt in enumerate(receipts, start=1):
        image_bytes = (image_fetcher or fetch_image_bytes)(receipt)
        if not image_bytes:
            stats["receipts_no_image"] += 1
            continue
        try:
            parsed = (ocr_call or _default_ocr_call)(prepare_image(image_bytes))
        except Exception:  # noqa: BLE001 - one bad receipt must not end the run
            logger.exception("receipt %s: line-items OCR failed", receipt.get("id"))
            stats["receipts_ocr_failed"] += 1
            continue
        stats["receipts_ocred"] += 1

        misses, per_receipt = collect_misses(receipt, parsed, conversions)
        all_misses.extend(misses)
        stats["lines"] += per_receipt["lines"]
        stats["no_unit"] += per_receipt["no_unit"]
        stats["misses"] += per_receipt["misses"]
        stats["subtotal_mismatch"] += int(per_receipt["subtotal_mismatch"])

        if index % 25 == 0:
            logger.info("… %d/%d receipts processed", index, len(receipts))

    return rank_worklist(all_misses), stats


def _default_ocr_call(image_bytes: bytes) -> dict:
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["ZAI_API_KEY"],
        base_url=os.environ.get("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
    )
    return call_line_items_ocr(client, image_bytes)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"lookback window (default {DEFAULT_DAYS})")
    parser.add_argument("--date-field", choices=("receipt_date", "created_at"),
                        default="receipt_date",
                        help="which date the window applies to (default receipt_date)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max receipts to select (default {DEFAULT_LIMIT}; 0 = no cap)")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="hard cap on OCR calls; the run stops there")
    parser.add_argument("--dry-run", action="store_true",
                        help="select receipts and report the call count, spend nothing")
    parser.add_argument("--allow-empty-conversions", action="store_true",
                        help="proceed even when uom_conversion has no rows")
    parser.add_argument("--out", default=None,
                        help="write the CSV here (default: stdout)")
    args = parser.parse_args(argv)

    rows, stats = run(
        build_client(),
        days=args.days,
        date_field=args.date_field,
        limit=args.limit,
        max_calls=args.max_calls,
        dry_run=args.dry_run,
        allow_empty_conversions=args.allow_empty_conversions,
    )

    print(format_report(rows, stats), file=sys.stderr)
    if args.dry_run:
        return 0

    csv_text = build_csv(rows)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
        print(f"\nWrote {len(rows)} row(s) to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(csv_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
