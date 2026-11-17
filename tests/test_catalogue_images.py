"""Images of the curated seed products and parts, served by the backend."""

import csv
import pathlib
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from index import CATALOGUE_IMAGES_DIR, CATALOGUE_IMAGES_PATH, SECURITY_HEADERS, app

client = TestClient(app)
SEED_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed"


def _seed_image_urls(file_name):
    with open(SEED_DIR / file_name, newline="", encoding="utf-8") as handle:
        return [(row["sku"], row["image_url"]) for row in csv.DictReader(handle)]


@pytest.mark.parametrize("file_name", ["partner_products.csv", "parts.csv"])
def test_every_seed_row_points_to_an_existing_catalogue_image(file_name):
    rows = _seed_image_urls(file_name)

    assert rows
    for sku, url in rows:
        parsed = urlparse(url)
        assert parsed.scheme == "https", sku
        assert parsed.path.startswith(CATALOGUE_IMAGES_PATH + "/"), sku
        relative = parsed.path[len(CATALOGUE_IMAGES_PATH) + 1:]
        assert (CATALOGUE_IMAGES_DIR / relative).is_file(), f"{sku}: missing {relative}"


def test_catalogue_image_is_public_and_cacheable():
    response = client.get(f"{CATALOGUE_IMAGES_PATH}/parts/drain-pump.webp")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_missing_catalogue_image_is_404_without_a_token():
    response = client.get(f"{CATALOGUE_IMAGES_PATH}/parts/does-not-exist.webp")

    assert response.status_code == 404


def test_catalogue_images_cannot_leave_their_folder():
    for path in ("%2e%2e/index.py", "..%2Findex.py", "%2e%2e/%2e%2e/api/index.py"):
        response = client.get(f"{CATALOGUE_IMAGES_PATH}/{path}")

        assert response.status_code == 404, path
