"""Listing photos (#36): validation, compression and storage.

* Uploads over 5 MB are refused; anything else is decoded, rotated by its EXIF
  orientation, scaled down to 1600 px wide and re-encoded as JPEG, so a 12 MP
  phone photo ends up around 200-500 KB.
* A listing holds at most ``MAX_IMAGES_PER_LISTING`` photos (checked on
  ``image_urls`` in the create and edit requests, see ``schemas``).
* Photos live in the ``listing_images`` Firestore collection (the Spark plan has
  no Cloud Storage bucket, see ``firebase_store.upload_listing_image``). When
  Firebase is not configured (local development) they go to a folder on disk
  instead, so the sell form can still be tried without credentials.
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

import firebase_store

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_LISTING = 6
MAX_WIDTH = 1600
# Qualities tried in order until the JPEG fits what Firestore can hold.
_QUALITIES = (82, 72, 62, 52, 42)
_LOCAL_ID = re.compile(r"^[a-f0-9]{32}$")


class ImageError(ValueError):
    """The upload cannot be used; ``status_code`` is the HTTP status to answer with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def compress(data: bytes) -> bytes:
    """Validate an uploaded photo and return it as a JPEG of at most 1600 px wide."""
    if not data:
        raise ImageError(422, "the image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(413, "the image is larger than 5 MB")
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ImageError(422, "the file is not a valid image") from exc

    if image.mode in ("RGBA", "LA", "P"):
        # JPEG has no transparency: flatten it onto white instead of black.
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    if image.width > MAX_WIDTH:
        image = image.resize((MAX_WIDTH, round(image.height * MAX_WIDTH / image.width)), Image.LANCZOS)

    while True:
        for quality in _QUALITIES:
            out = io.BytesIO()
            image.save(out, "JPEG", quality=quality, optimize=True)
            if out.tell() <= firebase_store.MAX_LISTING_IMAGE_BYTES:
                return out.getvalue()
        # Very noisy photo: shrink it further and try again.
        image = image.resize((round(image.width * 0.8), round(image.height * 0.8)), Image.LANCZOS)


# ---------- storage ----------

def _use_local() -> bool:
    mode = os.getenv("LISTING_IMAGES_BACKEND", "").strip().lower()
    if mode:
        return mode == "local"
    return not (os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip() or os.getenv("FIREBASE_PROJECT_ID", "").strip())


def _local_dir() -> pathlib.Path:
    default = pathlib.Path(__file__).resolve().parent.parent / ".local_uploads" / "listing_images"
    return pathlib.Path(os.getenv("LISTING_IMAGES_DIR", "").strip() or default)


def save(uid: str, jpeg: bytes) -> str:
    """Store a compressed photo and return its id."""
    if _use_local():
        directory = _local_dir()
        directory.mkdir(parents=True, exist_ok=True)
        image_id = uuid.uuid4().hex
        (directory / f"{image_id}.jpg").write_bytes(jpeg)
        return image_id
    return firebase_store.upload_listing_image(uid, jpeg, "image/jpeg")


def load(image_id: str) -> tuple[bytes, str] | None:
    """The stored photo as (bytes, content type), or None when it does not exist."""
    if _use_local():
        if not _LOCAL_ID.match(image_id or ""):
            return None
        path = _local_dir() / f"{image_id}.jpg"
        return (path.read_bytes(), "image/jpeg") if path.is_file() else None
    return firebase_store.get_listing_image(image_id)
