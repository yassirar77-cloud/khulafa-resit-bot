"""Registry for the 10 REAL shift-close fixtures (PR #35).

The ``.TXT`` files in ``tests/fixtures/sales/`` are the genuine POS shift-close
reports for the 25-May-2026 19:00 batch (UTF-16/BOM/CRLF), uploaded by the
owner. Filenames carry a dated, mixed-case suffix (``S-Damansara 25May2026.TXT``)
so tests resolve them by the ``S-<CODE>`` prefix and ignore the rest.

``EXPECTED`` holds the values verified against those files: totals, tax, and
which optional sections each report contains (matching the variance analysis —
stock for D.U/KLANG/SEK20/SEK6, deleted for KLANG/SEK14/SEK20/VISTA, cashdrawer
everywhere except SEK14/SEK20). All 10 are 19:00 "day" shifts dated 2026-05-25.
"""

from __future__ import annotations

import glob
import os

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "sales")

# code -> expected parse results (cross-checked against the real files).
EXPECTED = [
    {"code": "S-BISTRO7",   "canonical": "Bistro",       "total": 6563.75, "tax": 382.62,
     "has_stock": False, "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-DAMANSARA", "canonical": "D.U",          "total": 3086.00, "tax": 0.0,
     "has_stock": True,  "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-JAKEL",     "canonical": "Jakel",        "total": 2658.00, "tax": 0.0,
     "has_stock": False, "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-KLANG",     "canonical": "Klang B.Emas", "total": 4758.20, "tax": 0.0,
     "has_stock": True,  "has_deleted": True,  "has_cashdrawer": True},
    {"code": "S-SBESI",     "canonical": "SBESI",        "total": 2672.11, "tax": 0.0,
     "has_stock": False, "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-SEK14",     "canonical": "Signature",    "total": 7412.50, "tax": 0.0,
     "has_stock": False, "has_deleted": True,  "has_cashdrawer": False},
    {"code": "S-SEK15",     "canonical": "One Bistro",   "total": 3542.10, "tax": 0.0,
     "has_stock": False, "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-SEK20",     "canonical": "SEK-20",       "total": 4704.10, "tax": 0.0,
     "has_stock": True,  "has_deleted": True,  "has_cashdrawer": False},
    {"code": "S-SEK6",      "canonical": "SEK-6",        "total": 5162.50, "tax": 0.0,
     "has_stock": True,  "has_deleted": False, "has_cashdrawer": True},
    {"code": "S-VISTA",     "canonical": "Vista",        "total": 4108.40, "tax": 0.0,
     "has_stock": False, "has_deleted": True,  "has_cashdrawer": True},
]

EXPECTED_BUSINESS_DATE = "2026-05-25"
EXPECTED_SHIFT_TYPE = "day"
EXPECTED_GRAND_TOTAL = sum(e["total"] for e in EXPECTED)


def by_code(code):
    for e in EXPECTED:
        if e["code"] == code:
            return e
    raise KeyError(code)


def path_for_code(code) -> str:
    """Resolve the real fixture file for a subject code (``S-KLANG``), ignoring
    the dated, mixed-case suffix in the filename."""
    code = code.upper()
    for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.TXT"))):
        stem = os.path.basename(path).split(" ", 1)[0].upper()
        if stem == code:
            return path
    raise FileNotFoundError(f"No real fixture for {code} in {FIXTURE_DIR}")


def all_paths() -> list:
    return [path_for_code(e["code"]) for e in EXPECTED]
