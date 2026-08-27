"""Pagination helper for PostgREST reads.

Hosted Supabase clamps every request to its max-rows setting (1000 by
default) — including explicit ``.limit()`` values above it — so a plain
"select everything" silently truncates once a table grows past the cap.
``fetch_all_pages`` re-issues the query with ``.range()`` windows until a
short page arrives, so callers actually see every row. ``iter_pages`` is
the same walk exposed one page at a time, for callers (the CSV exports)
that can consume rows as they arrive instead of holding them all.
"""

from __future__ import annotations


def iter_pages(make_query, page_size: int = 1000):
    """Yield each page of a query as a list of rows.

    Same contract as ``fetch_all_pages`` — ``make_query`` must return a
    FRESH query builder on each call (builders are single-use) and include
    a deterministic ``.order()`` so pages don't shear against concurrent
    writes — but nothing accumulates here, so a caller that writes each
    page straight out never holds the whole table in memory.
    """
    start = 0
    while True:
        page = (
            make_query().range(start, start + page_size - 1).execute().data
            or []
        )
        if page:
            yield page
        if len(page) < page_size:
            return
        start += page_size


def fetch_all_pages(make_query, page_size: int = 1000) -> list:
    """Read every row of a query past the server page cap.

    ``make_query`` must return a FRESH query builder on each call (builders
    are single-use) and include a deterministic ``.order()`` so pages don't
    shear against concurrent writes.
    """
    rows: list = []
    for page in iter_pages(make_query, page_size):
        rows.extend(page)
    return rows
