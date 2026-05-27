"""Registry for the 7 REAL daily-summary (D-file) fixtures (PR #60).

The ``.TXT`` files in ``tests/fixtures/sales_daily/`` are genuine POS daily
summaries for 26-May-2026. Filenames carry a dated, mixed-case suffix
(``D-Damansara 26May2026.TXT``); resolve by the ``D-<CODE>`` prefix.

D-BISTRO7 / D-KLANG / D-VISTA are not yet uploaded — the parser must tolerate
their absence (only the 7 below are asserted).
"""

from __future__ import annotations

import glob
import os

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "sales_daily")

# code -> expected (day_sales, customers, total_shifts), verified against files.
EXPECTED = [
    {"code": "D-DAMANSARA", "sales": 3721.50, "customers": 482, "shifts": 2, "avg": 7.72},
    {"code": "D-JAKEL",     "sales": 4032.00, "customers": 435, "shifts": 2, "avg": 9.27},
    {"code": "D-SBESI",     "sales": 4767.90, "customers": 447, "shifts": 2, "avg": 10.67},
    {"code": "D-SEK14",     "sales": 8785.80, "customers": 550, "shifts": 2, "avg": 15.97},
    {"code": "D-SEK15",     "sales": 3551.50, "customers": 343, "shifts": 2, "avg": 10.35},
    {"code": "D-SEK20",     "sales": 8246.10, "customers": 662, "shifts": 2, "avg": 12.46},
    {"code": "D-SEK6",      "sales": 8620.60, "customers": 633, "shifts": 3, "avg": 13.62},
]


def by_code(code):
    for e in EXPECTED:
        if e["code"] == code:
            return e
    raise KeyError(code)


def path_for_code(code) -> str:
    code = code.upper()
    for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.TXT"))):
        stem = os.path.basename(path).split(" ", 1)[0].upper()
        if stem == code:
            return path
    raise FileNotFoundError(f"No D-fixture for {code} in {FIXTURE_DIR}")
