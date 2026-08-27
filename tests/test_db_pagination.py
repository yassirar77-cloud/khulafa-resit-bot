"""Unit tests for ``db_pagination``.

Hermetic — the page walk is exercised against the shared in-memory
``FakeSupabase`` double, which implements PostgREST's inclusive
``.range(start, end)``.

Run with::

    python -m unittest tests.test_db_pagination
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fake_supabase import FakeSupabase  # noqa: E402

from db_pagination import fetch_all_pages, iter_pages  # noqa: E402


def _client(n):
    client = FakeSupabase()
    for i in range(n):
        client.table("rows").insert({"id": i + 1, "n": i}).execute()
    return client


def _build(client):
    return lambda: client.table("rows").select("id, n").order("id", desc=False)


class IterPages(unittest.TestCase):
    def test_walks_every_page(self):
        client = _client(7)
        pages = list(iter_pages(_build(client), page_size=3))
        self.assertEqual([len(p) for p in pages], [3, 3, 1])
        self.assertEqual([r["n"] for p in pages for r in p], list(range(7)))

    def test_no_empty_trailing_page_on_exact_multiple(self):
        # 6 rows / page 3: the walk must stop instead of yielding a final
        # empty page a caller would have to guard against.
        pages = list(iter_pages(_build(_client(6)), page_size=3))
        self.assertEqual([len(p) for p in pages], [3, 3])

    def test_empty_table_yields_nothing(self):
        self.assertEqual(list(iter_pages(_build(_client(0)), page_size=3)), [])

    def test_nothing_accumulates_between_pages(self):
        # The point of iter_pages: a caller can consume and drop each page.
        seen = 0
        for page in iter_pages(_build(_client(5)), page_size=2):
            seen += len(page)
        self.assertEqual(seen, 5)


class FetchAllPages(unittest.TestCase):
    def test_returns_every_row_past_the_page_cap(self):
        rows = fetch_all_pages(_build(_client(7)), page_size=3)
        self.assertEqual([r["n"] for r in rows], list(range(7)))

    def test_empty_table(self):
        self.assertEqual(fetch_all_pages(_build(_client(0)), page_size=3), [])


if __name__ == "__main__":
    unittest.main()
