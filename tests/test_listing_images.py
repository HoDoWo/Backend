"""Listing photo compression, local fallback and cleanup (#36). Firestore is faked."""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import firebase_store
import listing_images
from index import app
from jwt_helpers import bearer, make_token
from test_firebase_store import FakeClient

API = "/api/marketplace/listings"


def raw(width, height, fmt="JPEG", mode="RGB"):
    out = io.BytesIO()
    Image.new(mode, (width, height), "blue").save(out, fmt)
    return out.getvalue()


def test_small_photos_are_not_enlarged():
    assert Image.open(io.BytesIO(listing_images.compress(raw(800, 600)))).size == (800, 600)


def test_transparent_png_becomes_a_white_background_jpeg():
    out = Image.open(io.BytesIO(listing_images.compress(raw(50, 50, "PNG", "RGBA"))))

    assert out.format == "JPEG"


def test_noisy_photo_still_fits_the_firestore_limit():
    noise = Image.frombytes("RGB", (900, 600), os.urandom(900 * 600 * 3))
    out = io.BytesIO()
    noise.save(out, "JPEG", quality=95)

    assert len(listing_images.compress(out.getvalue())) <= firebase_store.MAX_LISTING_IMAGE_BYTES


def test_empty_and_corrupt_uploads_are_refused():
    for data in (b"", b"garbage"):
        with pytest.raises(listing_images.ImageError) as error:
            listing_images.compress(data)
        assert error.value.status_code == 422


def test_without_firebase_photos_go_to_a_local_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("LISTING_IMAGES_BACKEND", "local")
    monkeypatch.setenv("LISTING_IMAGES_DIR", str(tmp_path))
    client = TestClient(app)

    url = client.post(f"{API}/images", headers=bearer(make_token()),
                      files={"image": ("a.png", raw(2000, 1000, "PNG"), "image/png")}).json()["url"]
    served = client.get(url.replace("http://testserver", ""))

    assert len(list(tmp_path.glob("*.jpg"))) == 1
    assert served.status_code == 200 and served.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(served.content)).size == (1600, 800)
    assert client.get(f"{API}/images/..%2Fsecret").status_code in (404, 422)
    assert client.get(f"{API}/images/{'0' * 32}").status_code == 404


@pytest.fixture
def fake_firestore(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: client)
    monkeypatch.setattr(firebase_store, "firestore_server_timestamp", lambda: "timestamp")
    return client


def test_deleting_a_listing_deletes_its_photos_but_only_the_ones_it_owns(fake_firestore):
    mine, theirs = "photo-mine", "photo-theirs"
    fake_firestore.store["listing_images"] = {
        mine: {"owner_uid": "seller-1", "data": b"jpg"},
        theirs: {"owner_uid": "seller-2", "data": b"jpg"},
    }
    base = "https://api.test/api/marketplace/listings/images/"
    fake_firestore.store["listings"] = {"l1": {
        "owner_uid": "seller-1",
        "image_urls": [base + mine, base + theirs, "https://example.test/external.jpg"],
    }}

    assert firebase_store.delete_marketplace_listing("seller-1", "l1") is True

    assert "l1" not in fake_firestore.store["listings"]
    assert mine not in fake_firestore.store["listing_images"]
    assert theirs in fake_firestore.store["listing_images"]


def test_listing_image_id_only_matches_our_urls():
    assert firebase_store.listing_image_id("https://x.test/api/marketplace/listings/images/abc_1") == "abc_1"
    assert firebase_store.listing_image_id("https://example.test/a.jpg") is None
