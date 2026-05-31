"""Best-effort durable archival of receipt photos to Cloudinary.

The live receipt flow must never break or stall because of image archival, so
``upload_receipt_image`` is deliberately fault-tolerant: it returns ``None`` on
*any* problem (missing SDK, missing credentials, network/Cloudinary error)
rather than raising. The caller stores the returned URL when present and simply
carries on otherwise — the Telegram ``photo_file_id`` remains as a free
fallback reference, so a failed upload never loses the receipt.

Configuration (already present in the Render env): either ``CLOUDINARY_URL``
(``cloudinary://<key>:<secret>@<cloud>``) or the individual
``CLOUDINARY_CLOUD_NAME`` / ``CLOUDINARY_API_KEY`` / ``CLOUDINARY_API_SECRET``
vars. ``CLOUDINARY_UPLOAD_FOLDER`` (default ``receipts``) sets the folder.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.environ.get("CLOUDINARY_UPLOAD_FOLDER", "receipts")


def _configured() -> bool:
    """True when Cloudinary credentials are present in the environment."""
    if os.environ.get("CLOUDINARY_URL"):
        return True
    return all(
        os.environ.get(k)
        for k in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    )


def upload_receipt_image(image_bytes: bytes, *, public_id: str | None = None) -> str | None:
    """Upload ``image_bytes`` to Cloudinary and return the secure URL, or None.

    Never raises. Returns ``None`` if the SDK isn't installed, credentials are
    absent, or the upload fails for any reason — the caller treats a missing URL
    as "archive unavailable, keep going".
    """
    if not image_bytes:
        return None
    if not _configured():
        logger.info("image_store: Cloudinary not configured; skipping upload")
        return None
    try:
        import cloudinary
        import cloudinary.uploader

        # If CLOUDINARY_URL is set the SDK self-configures from it; otherwise
        # wire up from the individual vars explicitly.
        if not os.environ.get("CLOUDINARY_URL"):
            cloudinary.config(
                cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
                api_key=os.environ["CLOUDINARY_API_KEY"],
                api_secret=os.environ["CLOUDINARY_API_SECRET"],
                secure=True,
            )

        opts = {"folder": UPLOAD_FOLDER, "resource_type": "image"}
        if public_id:
            opts["public_id"] = public_id
        result = cloudinary.uploader.upload(image_bytes, **opts)
        url = result.get("secure_url") or result.get("url")
        if url:
            logger.info("image_store: archived receipt image to Cloudinary (%s)", url)
        else:
            logger.warning("image_store: Cloudinary upload returned no URL: %r", result)
        return url
    except Exception:
        logger.warning("image_store: Cloudinary upload failed; continuing", exc_info=True)
        return None
