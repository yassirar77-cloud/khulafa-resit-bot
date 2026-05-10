"""Image preprocessing utilities for OCR.

Receipts uploaded via Telegram are often 2-5 MB phone photos at full sensor
resolution. The OCR model's attention degrades on huge images and latency
scales roughly with bytes-on-the-wire, so we downscale to a reasonable max
dimension before sending to the model. Production logs showed receipts at
77-520 KB taking 43-94s; resizing trims that to a few seconds while
typically improving extraction accuracy.
"""
from __future__ import annotations

import io
import logging
import time

logger = logging.getLogger(__name__)

SMALL_BYTES_THRESHOLD = 200 * 1024
SMALL_DIM_THRESHOLD = 1200


def resize_for_ocr(
    image_bytes: bytes,
    target_max_dim: int = 1600,
    jpeg_quality: int = 85,
) -> bytes:
    """Downscale an image so its longest side is ``target_max_dim``.

    Returns the original bytes unchanged when the input is already small
    (under ``SMALL_BYTES_THRESHOLD`` OR with both sides under
    ``SMALL_DIM_THRESHOLD``), or when decoding fails. Re-encodes resized
    output as JPEG at ``jpeg_quality``. EXIF orientation is applied so
    rotated phone photos stay upright after the format change strips EXIF.
    """
    original_size = len(image_bytes)
    start = time.monotonic()

    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("Pillow not available; skipping image resize")
        return image_bytes

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            original_dims = img.size  # (width, height)

            if (
                original_size < SMALL_BYTES_THRESHOLD
                or max(original_dims) < SMALL_DIM_THRESHOLD
            ):
                logger.info(
                    "resize_for_ocr: passthrough original_size_kb=%.1f original_dims=%dx%d",
                    original_size / 1024,
                    original_dims[0],
                    original_dims[1],
                )
                return image_bytes

            oriented = ImageOps.exif_transpose(img)
            oriented.thumbnail(
                (target_max_dim, target_max_dim), Image.Resampling.LANCZOS
            )
            resized_dims = oriented.size

            if oriented.mode not in ("RGB", "L"):
                oriented = oriented.convert("RGB")

            buf = io.BytesIO()
            oriented.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            resized_bytes = buf.getvalue()
    except Exception as exc:
        logger.warning(
            "resize_for_ocr: decode/resize failed (%s: %s); returning original bytes",
            type(exc).__name__,
            exc,
        )
        return image_bytes

    latency_ms = (time.monotonic() - start) * 1000.0
    logger.info(
        "resize_for_ocr: original_size_kb=%.1f resized_size_kb=%.1f "
        "original_dims=%dx%d resized_dims=%dx%d resize_latency_ms=%.1f",
        original_size / 1024,
        len(resized_bytes) / 1024,
        original_dims[0],
        original_dims[1],
        resized_dims[0],
        resized_dims[1],
        latency_ms,
    )
    return resized_bytes
