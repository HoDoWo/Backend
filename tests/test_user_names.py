"""Names users see of each other: the profile name when the token has none, never the email."""

from __future__ import annotations

import user_names
from fastapi.testclient import TestClient
from index import app
from jwt_helpers import bearer, make_token
from test_conversations import API as CONVERSATIONS
from test_conversations import BUYER, CONV, SELLER, clock, fridge, listings  # noqa: F401
from test_listings import API as LISTINGS
from test_listings import OTHER, store  # noqa: F401
from test_marketplace_search import db, search  # noqa: F401


def test_the_token_name_wins_then_the_profile_then_the_default(user_profiles):
    user_profiles["u1"] = "Laura B."

    assert user_names.of_user({"uid": "u1", "name": "  Laura   Bianchi "}) == "Laura Bianchi"
    assert user_names.of_user({"uid": "u1"}) == "Laura B."
    assert user_names.of_user({"uid": "u2"}) is None


def test_a_placeholder_is_replaced_by_the_owner_name_and_a_real_name_is_kept(user_profiles):
    user_profiles["u1"] = "Laura"

    assert user_names.resolve("Community seller", "u1", "Community seller") == "Laura"
    assert user_names.resolve(None, "u1", "Community seller") == "Laura"
    assert user_names.resolve("Davide", "u1", "Community seller") == "Davide"
    assert user_names.resolve("Community seller", "u9", "Community seller") == "Community seller"


def test_an_old_listing_shows_the_name_the_seller_set_later(store, user_profiles):
    store.listings["washer"]["seller_name"] = "Community seller"
    user_profiles["seller-1"] = "Giulia"
    other = TestClient(app, headers=bearer(make_token(sub=OTHER)))

    assert other.get(f"{LISTINGS}/washer").json()["seller_name"] == "Giulia"


def test_both_sides_of_a_chat_see_the_profile_name(listings, conversations, user_profiles):
    listings.listings["fridge"] = fridge(seller_name="Community seller")
    user_profiles[SELLER] = "Marco R."
    user_profiles[BUYER] = "Anna P."
    buyer = TestClient(app, headers=bearer(make_token(sub=BUYER)))
    seller = TestClient(app, headers=bearer(make_token(sub=SELLER)))

    assert buyer.post(CONVERSATIONS, json={"listing_id": "fridge", "text": "Hi!"}).json()["other_name"] == "Marco R."
    assert seller.get(f"{CONVERSATIONS}/{CONV}").json()["other_name"] == "Anna P."


REAL_FETCH_MANY = user_names._fetch_many


def test_the_cache_is_bounded_and_keeps_the_most_recent_names(user_profiles, monkeypatch):
    monkeypatch.setattr(user_names, "CACHE_SIZE", 2)
    user_profiles.update(a="A", b="B", c="C")

    assert user_names.lookup("a") == "A"
    assert user_names.lookup("b") == "B"
    assert user_names.lookup("a") == "A"  # "a" is now the most recent
    assert user_names.lookup("c") == "C"  # "b" is dropped

    assert list(user_names._cache) == ["a", "c"]
    assert user_profiles.calls == [["a"], ["b"], ["c"]]


def test_one_firestore_batch_then_one_auth_batch_for_the_names_still_missing(monkeypatch):
    import firebase_store
    from firebase_admin import auth as firebase_auth

    class Snapshot:
        def __init__(self, uid, data):
            self.id, self.exists, self._data = uid, data is not None, data

        def to_dict(self):
            return self._data

    profiles = {"p1": {"displayName": " Laura "}, "p2": {"displayName": ""}}
    asked = []

    class Client:
        def collection(self, name):
            assert name == "users"
            return self

        def document(self, uid):
            return uid

        def get_all(self, refs):
            asked.append(("firestore", list(refs)))
            return [Snapshot(uid, profiles.get(uid)) for uid in refs]

    class Record:
        def __init__(self, uid, name):
            self.uid, self.display_name = uid, name

    def get_users(identifiers):
        uids = [identifier.uid for identifier in identifiers]
        asked.append(("auth", uids))
        return type("Result", (), {"users": [Record("p3", "Marc")]})()

    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: Client())
    monkeypatch.setattr(firebase_auth, "get_users", get_users)

    names = REAL_FETCH_MANY(["p1", "p2", "p3"])

    assert names == {"p1": "Laura", "p2": None, "p3": "Marc"}
    assert asked == [("firestore", ["p1", "p2", "p3"]), ("auth", ["p2", "p3"])]


def test_no_firebase_means_no_names_and_no_error(monkeypatch):
    import firebase_store

    def broken():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(firebase_store, "get_firestore_client", broken)
    monkeypatch.setattr("firebase_admin.auth.get_users", lambda identifiers: (_ for _ in ()).throw(ValueError("no app")))

    assert REAL_FETCH_MANY(["x"]) == {}


def test_a_page_of_listings_names_its_sellers_in_one_batch(store, user_profiles):
    for listing in store.listings.values():
        listing["seller_name"] = "Community seller"
    other = TestClient(app, headers=bearer(make_token(sub=OTHER)))

    other.get(LISTINGS)

    assert len(user_profiles.calls) == 1


# ---------- marketplace search: names by collection and in batches ----------

def test_a_partner_product_without_type_keeps_its_partner_name(db, user_profiles):
    db.colls["parts"].docs["belt-1"].update(type="part", partner_name="Parts Co")
    user_profiles["Partner A"] = "Should not be used"

    sellers = {item["id"]: item["seller_name"] for item in search()["items"]}

    assert sellers["oven-1"] == "Partner A"  # no "type" in the document: still the partner
    assert sellers["belt-1"] == "Parts Co"  # a catalogue part with type "part"
    assert sellers["legacy"] == "Community seller"  # nobody to name: never a user lookup
    assert user_profiles.calls == [["seller-1"]]  # only the user listing was looked up


def test_the_sellers_of_a_page_are_named_in_one_batch(db, user_profiles):
    for n in range(5):
        db.colls["listings"].docs[f"l{n}"] = {
            "title": f"Item {n}", "status": "active", "price_cents": 100, "owner_uid": f"u{n % 3}",
            "seller_name": "Community seller", "created_at": 10 + n,
        }
    db.colls["listings"].docs["named"] = {
        "title": "Named", "status": "active", "price_cents": 100, "owner_uid": "u9",
        "seller_name": "Anna", "created_at": 9,
    }
    user_profiles.update(u0="Marco", u1="Giulia")

    items = {item["id"]: item for item in search()["items"]}

    assert len(user_profiles.calls) == 1
    assert sorted(user_profiles.calls[0]) == ["seller-1", "u0", "u1", "u2"]  # "u9" has a real name
    assert items["l0"]["seller_name"] == "Marco" and items["l1"]["seller_name"] == "Giulia"
    assert items["l2"]["seller_name"] == "Community seller" and items["named"]["seller_name"] == "Anna"
    assert all("owner_uid" not in item for item in items.values())

    search()  # cached: no second batch
    assert len(user_profiles.calls) == 1
