#!/usr/bin/env python3
"""SHADOW OCR summary — read-only verdict on Qwen vs GLM.

Reads ``ocr_shadow_comparison`` (populated by qwen_shadow_backfill.py) and
reports the evidence you need to decide whether Qwen3.6-Plus beats the live
GLM path:
  * how many receipts Qwen and GLM DISAGREE on for the total,
  * how many receipts Qwen scored MORE confidently than GLM,
  * the specific receipts where the totals differ most.

Pure analysis — touches no live tables, makes no API calls.

Usage:
  SUPABASE_URL=... SUPABASE_KEY=... python scripts/qwen_shadow_summary.py
  python scripts/qwen_shadow_summary.py --top 20 --tolerance 0.01
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr_shadow_fields  # noqa: E402

COMPARISON_TABLE = "ocr_shadow_comparison"
SHADOW_LOG_TABLE = "ocr_shadow_log"


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict], *, top: int = 10, tolerance: float = 0.01) -> dict:
    """Compute the comparison verdict from raw comparison rows.

    A "total disagreement" means both sides produced a total and they differ by
    more than ``tolerance`` (absolute). Receipts where either side has no total
    are counted separately (``missing_total``) rather than as agreement.
    """
    total_disagree = 0
    total_agree = 0
    missing_total = 0
    qwen_more_confident = 0
    deltas: list[dict] = []

    for r in rows:
        g_total = _to_float(r.get("glm_total"))
        q_total = _to_float(r.get("qwen_total"))
        if g_total is None or q_total is None:
            missing_total += 1
        else:
            delta = abs(g_total - q_total)
            if delta > tolerance:
                total_disagree += 1
                deltas.append(
                    {
                        "receipt_id": r.get("receipt_id"),
                        "glm_total": g_total,
                        "qwen_total": q_total,
                        "delta": delta,
                        "glm_confidence": r.get("glm_confidence"),
                        "qwen_confidence": r.get("qwen_confidence"),
                        "glm_date": r.get("glm_date"),
                        "qwen_date": r.get("qwen_date"),
                    }
                )
            else:
                total_agree += 1

        g_conf = r.get("glm_confidence")
        q_conf = r.get("qwen_confidence")
        if g_conf is not None and q_conf is not None and q_conf > g_conf:
            qwen_more_confident += 1

    deltas.sort(key=lambda d: d["delta"], reverse=True)
    return {
        "compared": len(rows),
        "total_disagree": total_disagree,
        "total_agree": total_agree,
        "missing_total": missing_total,
        "qwen_more_confident": qwen_more_confident,
        "biggest_deltas": deltas[:top],
    }


def format_summary(s: dict) -> str:
    lines = [
        "",
        "Qwen vs GLM — shadow OCR comparison",
        "=" * 60,
        f"receipts compared:            {s['compared']}",
        f"totals DISAGREE (>tolerance): {s['total_disagree']}",
        f"totals agree:                 {s['total_agree']}",
        f"one side had no total:        {s['missing_total']}",
        f"Qwen more confident than GLM: {s['qwen_more_confident']}",
        "",
        "Biggest total disagreements (GLM vs Qwen):",
    ]
    if not s["biggest_deltas"]:
        lines.append("  (none)")
    else:
        for d in s["biggest_deltas"]:
            lines.append(
                f"  receipt {d['receipt_id']}: glm={d['glm_total']} qwen={d['qwen_total']} "
                f"(Δ {d['delta']:.2f}) | conf glm={d['glm_confidence']} qwen={d['qwen_confidence']} "
                f"| date glm={d['glm_date']} qwen={d['qwen_date']}"
            )
    lines.append("")
    return "\n".join(lines)


def _pct(x):
    return "  —  " if x is None else f"{x * 100:5.1f}%"


def format_field_summary(s: dict) -> str:
    """Per-field (item/qty/unit/price) match rates + the switch decision."""
    lines = [
        "",
        "Qwen vs GLM — ordering-field accuracy (item / qty / unit / price)",
        "=" * 64,
        f"field rows logged: {s['rows']}   manual spot-checks: {s['manual_checked']}",
        "",
        f"  {'field':<6} {'agree(GLM=Qwen)':>16} {'GLM✓manual':>12} {'Qwen✓manual':>12}",
    ]
    for f in ocr_shadow_fields.FIELDS:
        pf = s["per_field"][f]
        lines.append(
            f"  {f:<6} {_pct(pf['agreement_rate']):>16} "
            f"{_pct(pf['glm_accuracy']):>12} {_pct(pf['qwen_accuracy']):>12}")
    lines += [
        "",
        f"VERDICT: {s['verdict']} — {s['decision']}",
        "",
    ]
    return "\n".join(lines)


def _build_client():
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="how many biggest deltas to list")
    parser.add_argument(
        "--tolerance", type=float, default=0.01,
        help="absolute total difference treated as agreement (default 0.01)",
    )
    parser.add_argument(
        "--fields", action="store_true",
        help="also report per-field (item/qty/unit/price) accuracy from ocr_shadow_log",
    )
    args = parser.parse_args()
    client = _build_client()
    rows = client.table(COMPARISON_TABLE).select("*").execute().data or []
    print(format_summary(summarize(rows, top=args.top, tolerance=args.tolerance)))
    if args.fields:
        field_rows = client.table(SHADOW_LOG_TABLE).select("*").execute().data or []
        print(format_field_summary(ocr_shadow_fields.score(field_rows)))


if __name__ == "__main__":
    main()
