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

import image_store  # noqa: E402
from image_store import upload_receipt_image  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
