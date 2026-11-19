"""Names users see of each other: the profile name when the token has none, never the email."""

from __future__ import annotations

from fastapi.testclient import TestClient

import user_names
from index import app
from jwt_helpers import bearer, make_token
from test_conversations import API as CONVERSATIONS, BUYER, CONV, SELLER, clock, fridge, listings  # noqa: F401
from test_listings import API as LISTINGS, OTHER, store  # noqa: F401


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


def test_an_old_listing_shows_the_name_the_seller_set_later(store, user_profiles):  # noqa: F811
    store.listings["washer"]["seller_name"] = "Community seller"
    user_profiles["seller-1"] = "Giulia"
    other = TestClient(app, headers=bearer(make_token(sub=OTHER)))

    assert other.get(f"{LISTINGS}/washer").json()["seller_name"] == "Giulia"


def test_both_sides_of_a_chat_see_the_profile_name(listings, conversations, user_profiles):  # noqa: F811
    listings.listings["fridge"] = fridge(seller_name="Community seller")
    user_profiles[SELLER] = "Marco R."
    user_profiles[BUYER] = "Anna P."
    buyer = TestClient(app, headers=bearer(make_token(sub=BUYER)))
    seller = TestClient(app, headers=bearer(make_token(sub=SELLER)))

    assert buyer.post(CONVERSATIONS, json={"listing_id": "fridge", "text": "Hi!"}).json()["other_name"] == "Marco R."
    assert seller.get(f"{CONVERSATIONS}/{CONV}").json()["other_name"] == "Anna P."
