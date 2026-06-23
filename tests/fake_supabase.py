"""Minimal in-memory Supabase double for tests.

Supports just the fluent subset the bot uses against simple tables:
``table(name).select(...).eq(col, val).limit(n).execute()`` and the matching
``insert`` / ``update().eq()`` / ``delete().eq()`` chains. ``execute()`` returns
an object with a ``.data`` list, like supabase-py.
"""

from __future__ import annotations

import itertools


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters: list[tuple[str, object]] = []
        self._null_cols: list[str] = []
        self._in_filters: list[tuple[str, set]] = []
        self._range_filters: list[tuple[str, str, object]] = []
        self._order: tuple[str, bool] | None = None
        self._limit = None
        self._range: tuple[int, int] | None = None

    # --- builders ---
    def select(self, *_cols):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None, **_kw):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = [c.strip() for c in on_conflict.split(",")] if on_conflict else []
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def is_(self, col, _val):
        # Only the IS NULL form is used by the bot.
        self._null_cols.append(col)
        return self

    def in_(self, col, vals):
        self._in_filters.append((col, set(vals)))
        return self

    def gte(self, col, val):
        self._range_filters.append((col, ">=", val))
        return self

    def lte(self, col, val):
        self._range_filters.append((col, "<=", val))
        return self

    def gt(self, col, val):
        self._range_filters.append((col, ">", val))
        return self

    def lt(self, col, val):
        self._range_filters.append((col, "<", val))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def range(self, start, end):
        # PostgREST range is inclusive on both ends.
        self._range = (start, end)
        return self

    # --- helpers ---
    def _matches(self, row):
        return (
            all(row.get(c) == v for c, v in self._filters)
            and all(row.get(c) is None for c in self._null_cols)
            and all(row.get(c) in vals for c, vals in self._in_filters)
            and all(self._cmp(row.get(c), op, v) for c, op, v in self._range_filters)
        )

    @staticmethod
    def _cmp(actual, op, val):
        if actual is None:
            return False
        if op == ">=":
            return actual >= val
        if op == "<=":
            return actual <= val
        if op == ">":
            return actual > val
        if op == "<":
            return actual < val
        return False

    def execute(self):
        rows = self._store.setdefault(self._table, [])
        if self._op == "select":
            out = [r for r in rows if self._matches(r)]
            if self._order is not None:
                col, desc = self._order
                out = sorted(out, key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
            if self._range is not None:
                start, end = self._range
                out = out[start:end + 1]
            if self._limit is not None:
                out = out[: self._limit]
            return _Result([dict(r) for r in out])
        if self._op == "insert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted = []
            for p in payloads:
                row = dict(p)
                row.setdefault("id", next(self._store["_ids"]))
                rows.append(row)
                inserted.append(dict(row))
            return _Result(inserted)
        if self._op == "upsert":
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            keys = getattr(self, "_on_conflict", []) or []
            result = []
            for p in payloads:
                existing = None
                if keys:
                    for r in rows:
                        if all(r.get(k) == p.get(k) for k in keys):
                            existing = r
                            break
                if existing is not None:
                    existing.update(p)
                    result.append(dict(existing))
                else:
                    row = dict(p)
                    row.setdefault("id", next(self._store["_ids"]))
                    rows.append(row)
                    result.append(dict(row))
            return _Result(result)
        if self._op == "update":
            changed = []
            for r in rows:
                if self._matches(r):
                    r.update(self._payload)
                    changed.append(dict(r))
            return _Result(changed)
        if self._op == "delete":
            kept = [r for r in rows if not self._matches(r)]
            removed = [r for r in rows if self._matches(r)]
            self._store[self._table] = kept
            return _Result([dict(r) for r in removed])
        raise AssertionError(f"unsupported op {self._op}")


class FakeSupabase:
    def __init__(self):
        self._store = {"_ids": itertools.count(1)}

    def table(self, name):
        return _Query(self._store, name)

    # test-only inspection helper
    def rows(self, name):
        return [dict(r) for r in self._store.get(name, [])]
