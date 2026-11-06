"""Official partner product API (#39): catalogue, partner-only writes, validation."""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

import catalogue_import
import partner_products
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakePartnerStore

API = "/api/marketplace/products"


def client_for(uid, **claims):
    return TestClient(app, headers=bearer(make_token(sub=uid, **claims)))


user = client_for("user-1", name="Davide")
volta = client_for("volta-staff", role="partner", partner_id="voltacasa")
nord = client_for("nord-staff", role="partner", partner_id="nordhome")
admin = client_for("admin-1", role="admin")


def product(partner_id, sku, **fields):
    doc = {
        "partner_id": partner_id, "partner_name": partner_id.title(), "partner_tier": 1, "sku": sku,
        "title": f"Product {sku}", "brand": "Bosch", "model": "M1", "category": "dishwasher",
        "description": "", "price_cents": 39900, "stock": 3, "image_url": None, "sponsored": False,
        "created_at": 1,
    }
    doc.update(fields)
    return f"{partner_id}_{sku}", doc


@pytest.fixture
def store(monkeypatch):
    fake = FakePartnerStore(
        partners={
            "voltacasa": {"name": "VoltaCasa", "partner_tier": 3, "active": True},
            "nordhome": {"name": "NordHome", "partner_tier": 2, "active": True},
            "gone": {"name": "Gone Ltd", "partner_tier": 1, "active": False},
        },
        products=dict([
            product("voltacasa", "VOL-1", title="Aurora washer", category="washing machine", partner_tier=3,
                    created_at=5),
            product("nordhome", "NOR-1", title="Nord dishwasher", partner_tier=2, sponsored=True, created_at=1),
            product("nordhome", "NOR-0", title="Sold out oven", category="oven", stock=0),
            product("gone", "GON-1", title="Inactive partner fridge", category="refrigerator"),
        ]),
    )
    clock = itertools.count(1_000)
    monkeypatch.setattr(partner_products, "get_store", lambda: fake)
    monkeypatch.setattr(catalogue_import, "now_ms", lambda: next(clock))
    return fake


NEW = {
    "sku": "VOL-2", "title": "Aurora dryer 8 kg", "brand": "Aurora", "model": "AD-8",
    "category": "dryer", "description": "Heat pump dryer.", "price": "449.90", "stock": 4,
    "image_url": "https://example.test/dryer.jpg",
}


# ---------- catalogue ----------

def test_the_catalogue_shows_in_stock_products_of_active_partners(store):
    items = user.get(API).json()["items"]

    # sponsored first, then higher tier
    assert [p["id"] for p in items] == ["nordhome_NOR-1", "voltacasa_VOL-1"]
    first = items[0]
    assert first["sponsored"] is True and first["partner_tier"] == 2
    assert first["price"] == 399.0 and first["price_cents"] == 39900
    assert first["type"] == "new" and first["in_stock"] is True


def test_filters_by_text_brand_category_and_partner(store):
    def ids(**params):
        return [p["id"] for p in user.get(API, params=params).json()["items"]]

    assert ids(q="aurora") == ["voltacasa_VOL-1"]
    assert ids(q="NOR-1") == ["nordhome_NOR-1"]
    assert ids(category="washing machine") == ["voltacasa_VOL-1"]
    assert ids(brand="bosch", partner="nordhome") == ["nordhome_NOR-1"]
    assert ids(brand="Miele") == []
    assert user.get(API, params={"category": "cars"}).status_code == 422


def test_a_partner_also_sees_its_own_products_out_of_stock(store):
    def ids(client, partner):
        return [p["id"] for p in client.get(API, params={"partner": partner}).json()["items"]]

    assert ids(user, "nordhome") == ["nordhome_NOR-1"]
    assert sorted(ids(nord, "nordhome")) == ["nordhome_NOR-0", "nordhome_NOR-1"]
    assert ids(volta, "nordhome") == ["nordhome_NOR-1"]  # not its own
    assert ids(admin, "gone") == ["gone_GON-1"]


def test_the_catalogue_is_paginated(store):
    for n in range(5):
        store.products[f"voltacasa_P{n}"] = product("voltacasa", f"P{n}", partner_tier=3, created_at=100 + n)[1]

    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = user.get(API, params=params).json()
        seen += [p["id"] for p in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == 7 and len(set(seen)) == 7
    assert seen[0] == "nordhome_NOR-1"
    assert user.get(API, params={"cursor": "nope"}).status_code == 422


def test_one_product_can_be_read(store):
    assert user.get(f"{API}/voltacasa_VOL-1").json()["title"] == "Aurora washer"
    assert user.get(f"{API}/missing").status_code == 404
    assert user.get(f"{API}/bad id").status_code == 422


# ---------- create ----------

def test_a_partner_creates_its_own_product(store):
    created = volta.post(API, json=NEW)

    assert created.status_code == 201
    body = created.json()
    assert body["id"] == "voltacasa_VOL-2"
    assert body["partner_name"] == "VoltaCasa" and body["partner_tier"] == 3  # from the partners document
    assert body["price_cents"] == 44990 and body["sponsored"] is False
    saved = store.products["voltacasa_VOL-2"]
    assert saved["created_by"] == "volta-staff"
    assert saved["title_normalised"] and saved["item_type"] == "new" and saved["condition"] == "new"
    assert "voltacasa_VOL-2" in [p["id"] for p in user.get(API).json()["items"]]


def test_a_regular_user_gets_403_on_any_write(store):
    assert user.post(API, json=NEW).status_code == 403
    assert user.patch(f"{API}/voltacasa_VOL-1", json={"stock": 0}).status_code == 403
    assert "voltacasa_VOL-2" not in store.products
    assert store.products["voltacasa_VOL-1"]["stock"] == 3


def test_a_partner_cannot_write_for_another_partner(store):
    assert nord.post(API, json={**NEW, "partner_id": "voltacasa"}).status_code == 403
    assert nord.patch(f"{API}/voltacasa_VOL-1", json={"price": "1.00"}).status_code == 403
    assert store.products["voltacasa_VOL-1"]["price_cents"] == 39900


def test_a_partner_role_without_partner_id_is_a_regular_user(store):
    broken = client_for("x", role="partner")

    assert broken.post(API, json=NEW).json()["detail"] == "only partners can manage products"


def test_an_admin_writes_for_any_partner_and_must_name_it(store):
    assert admin.post(API, json=NEW).status_code == 422
    created = admin.post(API, json={**NEW, "partner_id": "nordhome", "sponsored": True}).json()

    assert created["id"] == "nordhome_VOL-2" and created["sponsored"] is True
    assert admin.post(API, json={**NEW, "partner_id": "missing"}).status_code == 404


def test_skus_are_unique_per_partner_whatever_the_case(store):
    assert volta.post(API, json={**NEW, "sku": "VOL-1"}).status_code == 409
    assert volta.post(API, json={**NEW, "sku": "vol-1"}).status_code == 409
    assert nord.post(API, json={**NEW, "sku": "VOL-1"}).status_code == 201  # another partner may reuse it


def test_an_inactive_partner_cannot_write(store):
    gone = client_for("g", role="partner", partner_id="gone")

    assert gone.post(API, json=NEW).json()["detail"] == "this partner is not active"


@pytest.mark.parametrize(
    "change",
    [
        {"price": "0"},
        {"price": "-5"},
        {"price": "10.999"},
        {"price": "100000.01"},
        {"stock": -1},
        {"stock": 1.5},
        {"category": "car"},
        {"sku": "a/b"},
        {"sku": "x"},
        {"title": "ab"},
        {"listing_kind": "service"},
        {"partner_tier": 3},
        {"partner_name": "Fake"},
    ],
)
def test_create_validation(store, change):
    assert volta.post(API, json={**NEW, **change}).status_code == 422
    assert "voltacasa_VOL-2" not in store.products


def test_images_must_be_https(store):
    result = volta.post(API, json={**NEW, "image_url": "http://example.test/a.jpg"})

    assert result.status_code == 422 and result.json()["detail"] == "image_url must start with https://"


def test_only_an_admin_can_sponsor(store):
    assert volta.post(API, json={**NEW, "sponsored": True}).status_code == 403
    assert volta.post(API, json={**NEW, "sponsored": False}).status_code == 201
    assert volta.patch(f"{API}/voltacasa_VOL-1", json={"sponsored": True}).status_code == 403
    assert volta.patch(f"{API}/voltacasa_VOL-1", json={"sponsored": False}).status_code == 200  # unchanged value
    assert admin.patch(f"{API}/voltacasa_VOL-1", json={"sponsored": True}).json()["sponsored"] is True


# ---------- update ----------

def test_a_partner_updates_its_own_product(store):
    store.partners["voltacasa"]["partner_tier"] = 2  # changed since the import

    updated = volta.patch(f"{API}/voltacasa_VOL-1", json={"price": "349.00", "stock": 0, "title": " Aurora washer 9 kg "})

    assert updated.status_code == 200
    body = updated.json()
    assert body["price_cents"] == 34900 and body["stock"] == 0 and body["in_stock"] is False
    assert body["title"] == "Aurora washer 9 kg"
    assert body["partner_tier"] == 2
    saved = store.products["voltacasa_VOL-1"]
    assert saved["updated_by"] == "volta-staff" and saved["sku"] == "VOL-1"
    assert saved["title_normalised"] == "aurora washer 9 kg"


def test_update_validation(store):
    url = f"{API}/voltacasa_VOL-1"

    assert volta.patch(url, json={}).status_code == 422
    assert volta.patch(url, json={"sku": "NEW"}).status_code == 422  # the SKU is the id
    assert volta.patch(url, json={"partner_id": "nordhome"}).status_code == 422
    assert volta.patch(url, json={"category": "car"}).status_code == 422
    assert volta.patch(url, json={"image_url": "ftp://x"}).status_code == 422
    assert volta.patch(f"{API}/missing", json={"stock": 1}).status_code == 404


def test_the_catalogue_needs_a_token(store):
    assert TestClient(app).get(API).status_code == 401


def test_a_misconfigured_database_answers_503(monkeypatch):
    import firebase_store

    def broken():
        raise firebase_store.FirebaseConfigurationError("no credentials")

    monkeypatch.setattr(partner_products, "get_store", broken)

    result = user.get(API)
    assert result.status_code == 503 and result.json()["detail"] == "the catalogue is unavailable"
