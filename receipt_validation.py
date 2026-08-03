"""Arithmetic validation of an OCR'd invoice before its lines are trusted.

An invoice whose line totals do not add up to its printed subtotal has an OCR
error somewhere — a dropped line, a misread digit, a merged row. Which one is
unknowable from the numbers alone, so the response is never to "fix" it: the
receipt is stored, every line is marked ``needs_review``, ``qty_base`` is
withheld, and the receipt is posted back to the outlet's Telegram group for a
human to correct.

Two independent checks:

* :func:`check_subtotal` — Σ line_total vs the printed subtotal, tolerance
  RM0.02 (a two-sen rounding allowance; anything larger is a real discrepancy,
  not float noise). This is the receipt-level gate.
* :func:`check_line_arithmetic` — qty × unit_price vs that line's own
  line_total, same tolerance. Line-level, so one bad row flags itself instead
  of poisoning the whole invoice.

Pure functions with no Telegram/Supabase imports; ``bot.py`` does the posting.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Rounding allowance in RM. Sums differing by more than this are discrepancies.
SUBTOTAL_TOLERANCE = 0.02


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_subtotal(
    lines: list[dict],
    subtotal: Any,
    *,
    tolerance: float = SUBTOTAL_TOLERANCE,
) -> dict:
    """Compare Σ ``line_total`` against the printed ``subtotal``.

    Returns a dict:
      * ``checked``   — False when the check could not run (no subtotal
        printed, or no line carries a numeric line_total). An unrun check is
        NOT a pass and NOT a failure; it simply cannot flag anything.
      * ``line_sum``  — the sum of numeric line totals, or ``None`` if none.
      * ``subtotal``  — the subtotal as a float, or ``None``.
      * ``delta``     — ``line_sum - subtotal`` (``None`` when not checked).
      * ``needs_review`` — True iff the check ran and ``abs(delta) > tolerance``.
      * ``missing_line_totals`` — count of lines with no numeric line_total.

    Note that lines missing a line_total do NOT by themselves fail the check —
    they are reported so the flag message can say the sum is incomplete.
    """
    safe_lines = lines if isinstance(lines, list) else []
    numeric = [
        float(line.get("line_total"))
        for line in safe_lines
        if isinstance(line, dict) and _is_number(line.get("line_total"))
    ]
    missing = sum(
        1
        for line in safe_lines
        if not isinstance(line, dict) or not _is_number(line.get("line_total"))
    )
    line_sum = round(sum(numeric), 2) if numeric else None
    subtotal_f = float(subtotal) if _is_number(subtotal) else None

    if line_sum is None or subtotal_f is None:
        return {
            "checked": False,
            "line_sum": line_sum,
            "subtotal": subtotal_f,
            "delta": None,
            "needs_review": False,
            "missing_line_totals": missing,
        }

    delta = round(line_sum - subtotal_f, 2)
    needs_review = abs(delta) > tolerance
    if needs_review:
        logger.warning(
            "receipt_validation: line totals do not reconcile — sum=%.2f "
            "subtotal=%.2f delta=%.2f (tolerance=%.2f)",
            line_sum, subtotal_f, delta, tolerance,
        )
    return {
        "checked": True,
        "line_sum": line_sum,
        "subtotal": subtotal_f,
        "delta": delta,
        "needs_review": needs_review,
        "missing_line_totals": missing,
    }


def check_line_arithmetic(line: Any, *, tolerance: float = SUBTOTAL_TOLERANCE) -> bool:
    """True when ``qty × unit_price`` disagrees with this line's ``line_total``.

    Returns False when any of the three values is missing — an unverifiable
    line is not a wrong line (missing fields are handled by the confidence
    score instead).
    """
    if not isinstance(line, dict):
        return False
    qty = line.get("qty")
    unit_price = line.get("unit_price")
    line_total = line.get("line_total")
    if not (_is_number(qty) and _is_number(unit_price) and _is_number(line_total)):
        return False
    return abs(round(float(qty) * float(unit_price) - float(line_total), 2)) > tolerance


def format_subtotal_flag_message(
    result: dict,
    *,
    supplier: Any = None,
    invoice_no: Any = None,
    invoice_date: Any = None,
    outlet: Any = None,
    line_count: int = 0,
) -> str:
    """Build the Bahasa Malaysia message posted back to the outlet group.

    Written for the person holding the paper: it names the gap in ringgit and
    says what to do, rather than reporting a validation status.
    """
    delta = result.get("delta")
    line_sum = result.get("line_sum")
    subtotal = result.get("subtotal")
    parts = [
        "⚠️ Invois ini tak seimbang — sila semak.",
        "",
        f"Pembekal: {supplier or '—'}",
        f"Invois: {invoice_no or '—'}   Tarikh: {invoice_date or '—'}",
        f"Outlet: {outlet or '—'}",
        "",
        f"Jumlah {line_count} baris: RM{line_sum:.2f}" if line_sum is not None
        else f"Jumlah {line_count} baris: —",
        f"Subtotal tercetak: RM{subtotal:.2f}" if subtotal is not None
        else "Subtotal tercetak: —",
        f"Beza: RM{delta:+.2f}" if delta is not None else "Beza: —",
    ]
    missing = result.get("missing_line_totals") or 0
    if missing:
        parts.append(f"({missing} baris tiada jumlah — mungkin OCR terlepas.)")
    parts += [
        "",
        "Kuantiti baris TIDAK dikira untuk laporan wastage sehingga ini "
        "dibetulkan. Tolong hantar semula gambar yang lebih jelas, atau balas "
        "dengan nombor yang betul.",
    ]
    return "\n".join(parts)
