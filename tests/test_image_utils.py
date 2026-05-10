"""Unit tests for ``image_utils.resize_for_ocr``.

Run with::

    python -m unittest tests.test_image_utils
"""

import io
import logging
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from image_utils import resize_for_ocr  # noqa: E402


def _encode_jpeg(width: int, height: int, quality: int = 92, noisy: bool = False) -> bytes:
    """Build a deterministic JPEG so tests don't need fixture files.

    ``noisy=True`` produces high-entropy pixels so the encoded bytes are
    large enough to bypass the byte-size passthrough; the smooth gradient
    used otherwise compresses to under 200 KB even at large dimensions.
    """
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    if noisy:
        rng = random.Random(0xC0FFEE)
        for y in range(height):
            for x in range(width):
                pixels[x, y] = (
                    rng.randint(0, 255),
                    rng.randint(0, 255),
                    rng.randint(0, 255),
                )
    else:
        for y in range(height):
            for x in range(width):
                pixels[x, y] = (
                    (x * 255) // max(1, width - 1),
                    (y * 255) // max(1, height - 1),
                    128,
                )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _dims(image_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image_bytes)) as img:
        return img.size


class TinyImagePassthrough(unittest.TestCase):
    """Images under the byte threshold should pass through unchanged."""

    def test_tiny_image_under_200kb_unchanged(self):
        original = _encode_jpeg(800, 600, quality=70)
        self.assertLess(len(original), 200 * 1024)
        result = resize_for_ocr(original)
        self.assertIs(result, original)


class AlreadySmallPassthrough(unittest.TestCase):
    """Images below the dimension threshold should pass through unchanged
    even if (somehow) above the byte threshold."""

    def test_already_small_dims_unchanged(self):
        original = _encode_jpeg(1000, 800, quality=95)
        result = resize_for_ocr(original)
        self.assertIs(result, original)


class LargeLandscapeResize(unittest.TestCase):
    def test_3000x2000_resizes_to_1600x1067(self):
        original = _encode_jpeg(3000, 2000, quality=92, noisy=True)
        result = resize_for_ocr(original, target_max_dim=1600)
        self.assertNotEqual(result, original)
        w, h = _dims(result)
        self.assertEqual(w, 1600)
        self.assertEqual(h, 1067)
        self.assertLess(len(result), len(original))


class LargePortraitResize(unittest.TestCase):
    def test_2000x3000_resizes_to_1067x1600(self):
        original = _encode_jpeg(2000, 3000, quality=92, noisy=True)
        result = resize_for_ocr(original, target_max_dim=1600)
        w, h = _dims(result)
        self.assertEqual(w, 1067)
        self.assertEqual(h, 1600)


class VeryWideImageResize(unittest.TestCase):
    def test_4000x500_resizes_to_1600x200(self):
        original = _encode_jpeg(4000, 500, quality=92, noisy=True)
        result = resize_for_ocr(original, target_max_dim=1600)
        w, h = _dims(result)
        self.assertEqual(w, 1600)
        self.assertEqual(h, 200)


class CorruptedImageFallback(unittest.TestCase):
    """Corrupted/non-image bytes must not raise; fall back to original."""

    def test_corrupted_bytes_returned_as_is(self):
        # Make it large enough to bypass the size-based passthrough so we
        # actually exercise the decode path.
        garbage = b"not an image" * (20 * 1024)
        with self.assertLogs("image_utils", level="WARNING") as cm:
            result = resize_for_ocr(garbage)
        self.assertIs(result, garbage)
        self.assertTrue(any("decode/resize failed" in m for m in cm.output))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
