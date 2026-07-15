"""Pagination helper for PostgREST reads.

Hosted Supabase clamps every request to its max-rows setting (1000 by
default) — including explicit ``.limit()`` values above it — so a plain
"select everything" silently truncates once a table grows past the cap.
``fetch_all_pages`` re-issues the query with ``.range()`` windows until a
short page arrives, so callers actually see every row.
"""

from __future__ import annotations


def fetch_all_pages(make_query, page_size: int = 1000) -> list:
    """Read every row of a query past the server page cap.

    ``make_query`` must return a FRESH query builder on each call (builders
    are single-use) and include a deterministic ``.order()`` so pages don't
    shear against concurrent writes.
    """
    rows: list = []
    start = 0
    while True:
        page = (
            make_query().range(start, start + page_size - 1).execute().data
            or []
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        start += page_size
