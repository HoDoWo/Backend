"""Firestore side of the partner catalogue (#39): ids, one SKU per partner."""

from __future__ import annotations

import pytest

import firebase_store
import partner_store
from test_forum_store import Db


@pytest.fixture
def db(monkeypatch):
    fake = Db()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    return fake


def test_products_are_created_once_and_updated(db):
    assert partner_store.create_product("voltacasa_VOL-1", {"sku": "VOL-1", "stock": 3}) is True
    assert partner_store.create_product("voltacasa_VOL-1", {"sku": "VOL-1", "stock": 9}) is False

    partner_store.update_product("voltacasa_VOL-1", {"stock": 0})

    assert partner_store.get_product("voltacasa_VOL-1") == {"sku": "VOL-1", "stock": 0, "id": "voltacasa_VOL-1"}
    assert partner_store.get_product("missing") is None
    assert [p["id"] for p in partner_store.list_products()] == ["voltacasa_VOL-1"]


def test_partners_are_read_by_id(db):
    db.docs["partners/voltacasa"] = {"name": "VoltaCasa", "partner_tier": 3}

    assert partner_store.get_partner("voltacasa")["partner_tier"] == 3
    assert partner_store.get_partner("missing") is None
    assert partner_store.list_partners() == {"voltacasa": {"name": "VoltaCasa", "partner_tier": 3}}


def test_role_claims_replace_only_the_role_keys():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "set_user_role.py"
    spec = importlib.util.spec_from_file_location("set_user_role", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    current = {"admin": True, "role": "admin", "beta": True}
    assert script.new_claims(current, "voltacasa", False) == {"beta": True, "role": "partner", "partner_id": "voltacasa"}
    assert script.new_claims({"beta": True}, None, True) == {"beta": True, "role": "admin", "admin": True}
    assert script.new_claims(current, None, False) == {"beta": True}
    assert script.new_claims(None, None, False) == {}
