"""SQL tests for the Stok Dapur migration (RLS, PIN lockout, pre-fill, variance).

Runs tests/sql/test_stok_dapur.sql against a THROWAWAY local Postgres: set
STOK_TEST_PG to a libpq connection string for a server you can create
databases on, e.g.

    STOK_TEST_PG="host=/tmp port=54329 user=postgres" python -m unittest tests.test_stok_sql

Skipped when STOK_TEST_PG is unset or psql is missing (CI / Render), so the
normal suite never needs a database. Never point it at production: it creates
and drops its own database.
"""
import os
import shutil
import subprocess
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.environ.get("STOK_TEST_PG")

SETUP_FILES = ["tests/sql/supabase_stub.sql"]


def _psql(dsn, *args, sql=None):
    cmd = ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-X", *args]
    return subprocess.run(cmd, input=sql, capture_output=True, text=True, cwd=ROOT)


@unittest.skipUnless(DSN and shutil.which("psql"), "set STOK_TEST_PG to run the SQL tests")
class StokDapurSqlTest(unittest.TestCase):
    db = None

    @classmethod
    def setUpClass(cls):
        cls.db = "stok_test_" + uuid.uuid4().hex[:8]
        res = _psql(DSN, "-c", f"CREATE DATABASE {cls.db}")
        if res.returncode:
            raise unittest.SkipTest("cannot create a test database: " + res.stderr)
        cls.dsn = f"{DSN} dbname={cls.db}"
        for f in SETUP_FILES:
            res = _psql(cls.dsn, "-f", f)
            assert res.returncode == 0, f"{f}: {res.stderr}"

    @classmethod
    def tearDownClass(cls):
        if cls.db:
            _psql(DSN, "-c", f"DROP DATABASE IF EXISTS {cls.db} WITH (FORCE)")

    def test_01_migration_applies_twice(self):
        for _ in range(2):
            res = _psql(self.dsn, "-f", "migrations/0072_stok_dapur.sql")
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_02_behaviour_and_security(self):
        res = _psql(self.dsn, "-f", "tests/sql/test_stok_dapur.sql")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout[-2000:])
        self.assertIn("STOK SQL TESTS PASSED", res.stdout)


if __name__ == "__main__":
    unittest.main()
