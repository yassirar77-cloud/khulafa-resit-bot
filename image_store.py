"""Best-effort durable archival of receipt photos to Cloudinary.

The live receipt flow must never break or stall because of image archival, so
``upload_receipt_image`` is deliberately fault-tolerant: it returns ``None`` on
*any* problem (missing SDK, missing credentials, network/Cloudinary error)
rather than raising. The caller stores the returned URL when present and simply
carries on otherwise — the Telegram ``photo_file_id`` remains as a free
fallback reference, so a failed upload never loses the receipt.

``probe_cloudinary`` is a one-shot health check (run at startup and via the
``/cloudinary_check`` admin command) that makes a misconfigured or broken setup
loud instead of silent. It also never raises.

Configuration (already present in the Render env): either ``CLOUDINARY_URL``
(``cloudinary://<key>:<secret>@<cloud>``) or the individual
``CLOUDINARY_CLOUD_NAME`` / ``CLOUDINARY_API_KEY`` / ``CLOUDINARY_API_SECRET``
vars. ``CLOUDINARY_UPLOAD_FOLDER`` (default ``receipts``) sets the folder.
"""
from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

UPLOAD_FOLDER = os.environ.get("CLOUDINARY_UPLOAD_FOLDER", "receipts")

_TRIO = ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")

# Folder + a 1x1 transparent PNG embedded as bytes so the startup probe needs no
# image file and no Pillow — it just exercises the real upload path end to end.
PROBE_FOLDER = "cloudinary-probe"
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def _configured() -> bool:
    """True when Cloudinary credentials are present in the environment."""
    if os.environ.get("CLOUDINARY_URL"):
        return True
    return all(os.environ.get(k) for k in _TRIO)


def _missing_vars() -> list[str]:
    """Names (never values) of the env vars needed for archival but absent.

    Returns ``[]`` when configured via either form. When the individual vars
    are partially set, lists exactly the missing ones; when nothing is set,
    points at the simpler single-var alternative.
    """
    if _configured():
        return []
    missing = [k for k in _TRIO if not os.environ.get(k)]
    if len(missing) == len(_TRIO):
        return [f"CLOUDINARY_URL (or all of: {', '.join(_TRIO)})"]
    return missing


def _configure_sdk():
    """Import and configure the Cloudinary SDK from the environment.

    Returns the ``cloudinary`` module. Raises if the SDK isn't installed or the
    individual-var config is incomplete — callers wrap this. If ``CLOUDINARY_URL``
    is set the SDK self-configures from it; otherwise we wire up the trio.
    """
    import cloudinary
    import cloudinary.uploader  # noqa: F401  (ensures the submodule is imported)

    if not os.environ.get("CLOUDINARY_URL"):
        cloudinary.config(
            cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
            api_key=os.environ["CLOUDINARY_API_KEY"],
            api_secret=os.environ["CLOUDINARY_API_SECRET"],
            secure=True,
        )
    return cloudinary


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
        cloudinary = _configure_sdk()
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


def probe_cloudinary() -> tuple[bool, str]:
    """One-shot Cloudinary health check. Never raises; returns ``(ok, detail)``.

    ``ok`` is determined SOLELY by whether the test upload succeeds — config
    presence and a real round-trip to Cloudinary. The probe asset is deleted
    afterwards on a strict best-effort basis: a failed cleanup is swallowed and
    does NOT change the result, because archival working is what matters.
    """
    if not _configured():
        missing = ", ".join(_missing_vars())
        return False, f"not configured — receipt images will NOT be archived (missing: {missing})"
    try:
        cloudinary = _configure_sdk()
        result = cloudinary.uploader.upload(
            _PROBE_PNG,
            folder=PROBE_FOLDER,
            public_id="startup-probe",
            resource_type="image",
            overwrite=True,
        )
    except Exception as exc:
        return False, f"upload FAILED — images will NOT be archived: {exc}"

    # Upload succeeded → archival is live. Clean up the probe asset best-effort;
    # any failure here is intentionally ignored and must not flip the result.
    public_id = result.get("public_id")
    if public_id:
        try:
            cloudinary.uploader.destroy(public_id, resource_type="image")
        except Exception:
            logger.debug(
                "image_store: probe asset cleanup failed (harmless)", exc_info=True
            )
    return True, "OK — archival is live"
