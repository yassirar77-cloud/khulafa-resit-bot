"""Tests for best-effort Cloudinary receipt-image archival.

The contract that matters for the live flow: upload_receipt_image must NEVER
raise and must return None whenever archival can't happen (no creds, no SDK,
empty bytes, or any upload error), so a failed archive can't break or stall a
receipt.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import image_store  # noqa: E402
from image_store import probe_cloudinary, upload_receipt_image  # noqa: E402

_CLOUDINARY_VARS = (
    "CLOUDINARY_URL",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)


class ImageStoreTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _CLOUDINARY_VARS}
        for k in _CLOUDINARY_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_empty_bytes_returns_none(self):
        self.assertIsNone(upload_receipt_image(b""))

    def test_not_configured_returns_none(self):
        self.assertFalse(image_store._configured())
        self.assertIsNone(upload_receipt_image(b"fakebytes"))

    def test_configured_via_individual_vars(self):
        os.environ["CLOUDINARY_CLOUD_NAME"] = "c"
        os.environ["CLOUDINARY_API_KEY"] = "k"
        os.environ["CLOUDINARY_API_SECRET"] = "s"
        self.assertTrue(image_store._configured())

    def test_configured_via_url(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        self.assertTrue(image_store._configured())

    def test_upload_error_returns_none_never_raises(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        fake = mock.MagicMock()
        fake.uploader.upload.side_effect = RuntimeError("network down")
        with mock.patch.dict(
            sys.modules, {"cloudinary": fake, "cloudinary.uploader": fake.uploader}
        ):
            self.assertIsNone(upload_receipt_image(b"fakebytes"))

    def test_successful_upload_returns_secure_url(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        fake = mock.MagicMock()
        fake.uploader.upload.return_value = {"secure_url": "https://res.cloudinary.com/x.jpg"}
        with mock.patch.dict(
            sys.modules, {"cloudinary": fake, "cloudinary.uploader": fake.uploader}
        ):
            self.assertEqual(
                upload_receipt_image(b"fakebytes"), "https://res.cloudinary.com/x.jpg"
            )


class MissingVarsTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _CLOUDINARY_VARS}
        for k in _CLOUDINARY_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_configured_via_url_reports_nothing_missing(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        self.assertEqual(image_store._missing_vars(), [])

    def test_full_trio_reports_nothing_missing(self):
        os.environ["CLOUDINARY_CLOUD_NAME"] = "c"
        os.environ["CLOUDINARY_API_KEY"] = "k"
        os.environ["CLOUDINARY_API_SECRET"] = "s"
        self.assertEqual(image_store._missing_vars(), [])

    def test_nothing_set_points_at_single_var_alternative(self):
        missing = image_store._missing_vars()
        self.assertEqual(len(missing), 1)
        self.assertIn("CLOUDINARY_URL", missing[0])

    def test_partial_trio_lists_exactly_the_missing_ones(self):
        os.environ["CLOUDINARY_CLOUD_NAME"] = "c"
        self.assertEqual(
            image_store._missing_vars(),
            ["CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"],
        )

    def test_missing_vars_never_contains_secret_values(self):
        # Names only, never values — guard against accidental value leakage.
        os.environ["CLOUDINARY_CLOUD_NAME"] = "c"
        os.environ["CLOUDINARY_API_KEY"] = "supersecretkey"
        joined = " ".join(image_store._missing_vars())
        self.assertNotIn("supersecretkey", joined)


class ProbeCloudinaryTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _CLOUDINARY_VARS}
        for k in _CLOUDINARY_VARS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_not_configured_returns_false_with_missing_names(self):
        ok, detail = probe_cloudinary()
        self.assertFalse(ok)
        self.assertIn("not configured", detail)
        self.assertIn("CLOUDINARY_URL", detail)

    def test_upload_failure_returns_false_never_raises(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        fake = mock.MagicMock()
        fake.uploader.upload.side_effect = RuntimeError("401 unauthorized")
        with mock.patch.dict(
            sys.modules, {"cloudinary": fake, "cloudinary.uploader": fake.uploader}
        ):
            ok, detail = probe_cloudinary()
        self.assertFalse(ok)
        self.assertIn("upload FAILED", detail)
        self.assertIn("401 unauthorized", detail)

    def test_successful_upload_reports_ok_and_cleans_up(self):
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        fake = mock.MagicMock()
        fake.uploader.upload.return_value = {"public_id": "cloudinary-probe/startup-probe"}
        with mock.patch.dict(
            sys.modules, {"cloudinary": fake, "cloudinary.uploader": fake.uploader}
        ):
            ok, detail = probe_cloudinary()
        self.assertTrue(ok)
        self.assertEqual(detail, "OK — archival is live")
        fake.uploader.destroy.assert_called_once()

    def test_cleanup_failure_does_not_flip_success(self):
        # A successful upload followed by a failed destroy() must still be OK —
        # archival working is what matters; only the upload decides pass/fail.
        os.environ["CLOUDINARY_URL"] = "cloudinary://k:s@cloud"
        fake = mock.MagicMock()
        fake.uploader.upload.return_value = {"public_id": "cloudinary-probe/startup-probe"}
        fake.uploader.destroy.side_effect = RuntimeError("cleanup boom")
        with mock.patch.dict(
            sys.modules, {"cloudinary": fake, "cloudinary.uploader": fake.uploader}
        ):
            ok, detail = probe_cloudinary()
        self.assertTrue(ok)
        self.assertEqual(detail, "OK — archival is live")


class BotProbeWiringTests(unittest.TestCase):
    """Source-level checks that bot.py wires the probe (bot.py can't be imported
    in CI — runtime deps + required env vars), mirroring test_bot_review_flow."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "bot.py")) as f:
            cls.src = f.read()

    def test_imports_probe(self):
        self.assertIn("from image_store import probe_cloudinary, upload_receipt_image", self.src)

    def test_startup_runs_probe_offthread_and_logs(self):
        self.assertIn("await asyncio.to_thread(probe_cloudinary)", self.src)
        self.assertIn('"CLOUDINARY PROBE: %s"', self.src)

    def test_admin_command_registered_and_gated(self):
        self.assertIn('CommandHandler("cloudinary_check", cloudinary_check_command)', self.src)
        self.assertIn("async def cloudinary_check_command", self.src)
        # Gated with the same reviewer check as the other admin commands.
        cmd_idx = self.src.index("async def cloudinary_check_command")
        body = self.src[cmd_idx:cmd_idx + 600]
        self.assertIn("is_reviewer(_command_owner_id(update))", body)


if __name__ == "__main__":
    unittest.main()
