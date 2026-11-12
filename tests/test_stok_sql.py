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

    def test_03_item_seed_matches_real_receipt_lines(self):
        # own database: the behaviour test above leaves fixture items behind
        db = "stok_seed_" + uuid.uuid4().hex[:8]
        self.assertEqual(_psql(DSN, "-c", f"CREATE DATABASE {db}").returncode, 0)
        self.addCleanup(_psql, DSN, "-c", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")
        dsn = f"{DSN} dbname={db}"
        for f in SETUP_FILES + ["migrations/0072_stok_dapur.sql", "migrations/0073_stok_items_seed.sql",
                                "migrations/0073_stok_items_seed.sql"]:
            res = _psql(dsn, "-f", f)
            self.assertEqual(res.returncode, 0, res.stderr)
        # (line as OCR'd, merchant) -> expected stok item ('' = must NOT match)
        cases = [
            ("WHOLE LEG", "BESTARI FARM (M) SDN BHD", "ayam_whole_leg"),
            ("WHOLE LEG", "AYAM BERLIAN SDN BHD", ""),          # Ayam = exact Bestari only
            ("SUPERCH P8", "BESTARI FARM (M) SDN BHD", "ayam_p8"),
            ("LAI AYAM 4 KG", "BESTARI FARM (M) SDN BHD", "ayam_separuh_4kg"),
            ("WHOLE LEG (40 EKOR)", "BESTARI FARM (M) SDN BHD", "ayam_whole_leg"),
            ("BEEF 3 KG X3 RM22.5", "BESTARI FARM (M) SDN BHD", "daging_beef_3kg"),
            ("MUTTON MYSURE 5 KG", "BESTARI FARM (M) SDN BHD", "kambing_mysore"),
            ("BEEF TAILS ( ARG/BRZ/AUS) -EKOR", "MD HANEE FROZEN AND SEAFOODS", "daging_ekor"),
            ("INDIAN SILVERSIDE 44 T", "MD HANEE FROZEN AND SEAFOODS", "daging_silverside"),
            ("BP 5% - JASMINE SUPER BLUE 10KG", "JASMINE FOOD CORPORATION SDN. BHD.", "beras_jasmine_blue"),
            ("ULUNTHU DOSAI ANCHOR 15KG", "BESTARI WHOLESALE SDN BHD", "ulunthu"),
            ("URAD GOTA JM 10KG", "BESTARI WHOLESALE SDN BHD", "urad_gota"),
            ("DUCHEF TOMATO PUREE(430GM)", "BALAJI ENTERPRISE SDN BHD", "puri_tomato"),
            ("BABAS GARAM MASALA MIX 250G", "BABA PRODUCTS (M) SDN BHD", "garam_masala"),
            ("CUMIN SEED POWDER", "RK MUBARAKA SDN BHD", "serbuk_jintan_putih"),
            ("B. J. PUNN", "RK MUBARAKA SDN BHD", "jintan_putih_biji"),
            ("BLACK PEPPER POWDER", "RK MUBARAKA SDN BHD", "serbuk_lada_hitam"),
            ("B. L. HTRAM", "RK MUBARAKA SDN BHD", "lada_hitam_biji"),
            ("SERAI LALAT", "GEDA TRADING", ""),               # mosquito coil, not serai
            ("AIR LIMAU", "GEDA TRADING", ""),
            ("TOMATO", "GEDA TRADING", "tomato"),
            ("MUTTON MINCE 5 KG", "BESTARI FARM (M) SDN BHD", ""),   # not daging cincang
            ("SALAD (NO TOMATO, CUCUMBER)", "LAI ENTERPRISE SDN BHD", ""),
        ]
        values = ",".join("('%s','%s','%s')" % (n.replace("'", "''"), m, e) for n, m, e in cases)
        res = _psql(dsn, "-At", "-c",
                    "SELECT n || ' -> ' || coalesce(public.stok_match_item(n, m), '') || ' (want ' || e || ')' "
                    f"FROM (VALUES {values}) t(n, m, e) WHERE coalesce(public.stok_match_item(n, m), '') <> e")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "", "mismatched lines:\n" + res.stdout)
        res = _psql(dsn, "-At", "-c",
                    "SELECT count(*) FILTER (WHERE active), count(DISTINCT category), "
                    "count(*) FILTER (WHERE category = 'ayam' AND unit = 'pcs' AND terima_unit = 'kg') "
                    "FROM public.stok_items")
        self.assertEqual(res.stdout.strip(), "81|8|5")


if __name__ == "__main__":
    unittest.main()
