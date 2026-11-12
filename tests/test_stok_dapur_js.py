"""Runs the Stok Dapur PWA's JavaScript unit tests (stok_dapur/tests/core.test.js)
with node's built-in test runner. Skipped where node is not installed."""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("node"), "node not installed")
class StokDapurJsTest(unittest.TestCase):
    def test_core_js(self):
        res = subprocess.run(["node", "--test", "stok_dapur/tests/core.test.js"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout[-3000:] + res.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
