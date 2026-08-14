#!/usr/bin/env python3
"""READ-ONLY backtest: would the cook-to-demand forecast have been right?

Replays ``demand_forecast`` over the kitchen-usage history the bot already has.
For every (outlet, item, day) with enough prior history, the forecast is rebuilt
using ONLY the days before it — the same walk-forward the live 11:00 job does —
and compared with what that day actually demanded. Two verdicts are printed:

  * **forecast accuracy** — median absolute percentage error, and how often the
    forecast landed within 10 % / 20 % of the real demand.
  * **what the recommendation would have changed** — against what the shop
    ACTUALLY cooked that day, how much leftover the recommendation would have
    avoided (over-cook days) and how often it would have covered a day the shop
    sold out on (under-cook days).

The second number is the one that matters at the stove: a forecast can be
accurate and still useless if it never differs from what the kitchen already
does. Days the shop sold out are reported separately — their recorded demand is
censored (the real demand was higher), so an error measured against them is a
floor, not a fact.

Nothing is written — only ``.select()`` queries.

Run on the Render shell (or locally with the prod env vars)::

    SUPABASE_URL=... SUPABASE_KEY=... python scripts/backtest_demand_forecast.py
    python scripts/backtest_demand_forecast.py --days 120 --outlet SEK6
    python scripts/backtest_demand_forecast.py --item ayam_goreng --verbose
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import demand_forecast as df  # noqa: E402
import kitchen_usage as ku  # noqa: E402

DEFAULT_DAYS = 120


def _build_client():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_KEY (read-only).")
    return create_client(url, key)


def _outlet_codes(rows: list[dict]) -> list[str]:
    return sorted({str(r.get("outlet_code") or "") for r in rows if r.get("outlet_code")})


def _median(values):
    return round(statistics.median(values), 1) if values else None


def backtest(rows: list[dict], outlets: list[str], item_filter: str | None,
             verbose: bool) -> dict:
    """Walk-forward replay over every outlet/item/day with enough prior history."""
    stats = {
        "n": 0, "errors": [], "within_10": 0, "within_20": 0,
        "sold_out": 0,
        # recommendation-vs-reality
        "leftover_saved": [], "leftover_days": 0,
        "sellout_covered": 0, "sellout_days": 0,
        "per_outlet": {}, "per_item": {},
    }
    for outlet in outlets:
        series = df.demand_series(rows, outlet)
        for item_code, points in sorted(series.items()):
            if item_filter and item_code != item_filter:
                continue
            unit = ku.ITEM_BY_CODE.get(item_code, {}).get("unit", "pcs")
            for day_iso in sorted(points):
                try:
                    target = date.fromisoformat(day_iso)
                except ValueError:
                    continue
                fc = df.forecast_item(item_code, points, target)
                if fc is None:
                    continue          # not enough history yet at that point
                actual_point = points[day_iso]
                actual = float(actual_point["demand"])
                censored = bool(actual_point.get("censored"))
                cooked = actual_point.get("cooked")
                left = actual_point.get("left")

                stats["n"] += 1
                if censored:
                    stats["sold_out"] += 1
                if actual > 0:
                    pct = abs(actual - fc["forecast"]) / actual * 100.0
                    stats["errors"].append(pct)
                    stats["per_outlet"].setdefault(outlet, []).append(pct)
                    stats["per_item"].setdefault(item_code, []).append(pct)
                    if pct <= 10.0:
                        stats["within_10"] += 1
                    if pct <= 20.0:
                        stats["within_20"] += 1

                # What the recommendation would have changed that day.
                if cooked is not None and left is not None and left > 0:
                    # Leftover day: the surplus the recommendation would have cut,
                    # never below the demand that actually showed up. Collected
                    # per day and reported as a MEDIAN — the first version summed
                    # these raw and reported "306,995 saved" off the back of a
                    # handful of 2000-pc and 10900-kg numpad errors.
                    if fc["recommend"] < float(cooked):
                        saved = float(cooked) - max(fc["recommend"], actual)
                        if saved > 0:
                            stats["leftover_saved"].append(saved)
                            stats["leftover_days"] += 1
                if censored and cooked is not None:
                    stats["sellout_days"] += 1
                    if fc["recommend"] > float(cooked):
                        stats["sellout_covered"] += 1

                if verbose:
                    print(
                        f"  {outlet:8s} {day_iso} {item_code:14s} "
                        f"forecast {fc['forecast']:7.1f} rec {fc['recommend']:6.1f} "
                        f"actual {actual:7.1f} {unit}"
                        f"{'  SOLD OUT' if censored else ''}"
                    )
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"history window to replay (default {DEFAULT_DAYS})")
    ap.add_argument("--outlet", help="limit to one kitchen outlet code")
    ap.add_argument("--item", help="limit to one item_code, e.g. ayam_goreng")
    ap.add_argument("--verbose", action="store_true",
                    help="print every replayed day")
    args = ap.parse_args()

    client = _build_client()
    end = date.today()
    start = end - timedelta(days=args.days)
    rows = df.load_usage_rows(client, start.isoformat(), end.isoformat())
    if not rows:
        raise SystemExit("No kitchen_daily_usage rows in that window.")

    outlets = [args.outlet.strip().upper()] if args.outlet else _outlet_codes(rows)
    print(
        f"Backtesting {start} .. {end} — {len(rows)} usage rows, "
        f"{len(outlets)} outlet(s)\n"
    )
    stats = backtest(rows, outlets, args.item, args.verbose)

    n = stats["n"]
    if not n:
        raise SystemExit(
            "Nothing replayable — every item is under "
            f"{df.MIN_DATA_DAYS} data days of history."
        )
    print(f"\nForecast accuracy ({n} item-days)")
    print(f"  median error : {_median(stats['errors'])}%")
    scored = len(stats["errors"])
    if scored:
        print(f"  within 10%   : {stats['within_10']}/{scored} "
              f"({round(stats['within_10'] / scored * 100)}%)")
        print(f"  within 20%   : {stats['within_20']}/{scored} "
              f"({round(stats['within_20'] / scored * 100)}%)")
    print(f"  sold-out days: {stats['sold_out']} "
          "(demand censored — the error there is a floor)")

    print("\nWhat the recommendation would have changed")
    print(f"  over-cook days it would have trimmed : {stats['leftover_days']}")
    print(f"  leftover avoided per such day (median): "
          f"{_median(stats['leftover_saved'])}")
    if stats["sellout_days"]:
        print(f"  sell-out days it would have covered  : "
              f"{stats['sellout_covered']}/{stats['sellout_days']}")
    else:
        print("  sell-out days it would have covered  : none in window")

    if stats["per_item"]:
        print("\nPer item (median error) — items under the volume floor are skipped")
        for item, errs in sorted(
            stats["per_item"].items(), key=lambda kv: -statistics.median(kv[1])
        ):
            print(f"  {item:16s} {_median(errs)}%  ({len(errs)} item-days)")

    if stats["per_outlet"]:
        print("\nPer outlet (median error)")
        for outlet, errs in sorted(
            stats["per_outlet"].items(), key=lambda kv: -statistics.median(kv[1])
        ):
            print(f"  {outlet:10s} {_median(errs)}%  ({len(errs)} item-days)")


if __name__ == "__main__":
    main()
