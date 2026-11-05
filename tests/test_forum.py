"""Appliance forum API (#44): categories, threads, replies, edits, votes, reports, moderation."""

from __future__ import annotations

import itertools

import pytest
from fastapi.testclient import TestClient

import checkout_service
import forum_service
from index import app
from jwt_helpers import bearer, make_token
from marketplace_fakes import FakeForumStore, FakeStore

ASKER, HELPER, SELLER = "asker-1", "helper-2", "seller-9"
API = "/api/forum"
WASHERS = f"{API}/categories/washing-machine/threads"
PUMP = f"{API}/products/part/pump-1/threads"


def client_for(uid, name=None, **claims):
    return TestClient(app, headers=bearer(make_token(sub=uid, name=name, **claims)))


asker = client_for(ASKER, "Davide P.")
helper = client_for(HELPER, "Anna")
seller = client_for(SELLER, "Marc")
admin = client_for("admin-1", "Moderator", admin=True)


@pytest.fixture
def forum(monkeypatch):
    fake = FakeForumStore()
    clock = itertools.count(1_000)
    monkeypatch.setattr(forum_service, "get_store", lambda: fake)
    monkeypatch.setattr(checkout_service, "now_ms", lambda: next(clock))
    return fake


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(checkout_service, "get_store", lambda: fake)
    fake.items = {
        ("part", "pump-1"): {"title": "Drain pump", "category": "washing machine", "brand": "Bosch",
                             "model": "WAN28209GB", "price_cents": 2500, "stock": 3, "partner_id": "partner-a"},
        ("used", "fridge-2"): {"title": "Used fridge", "category": "refrigerator", "price_cents": 12000,
                               "status": "active", "owner_uid": SELLER},
        ("used", "sold-tv"): {"title": "Sold TV", "category": "tv", "price_cents": 8000, "status": "sold",
                              "owner_uid": SELLER},
    }
    return fake


def new_thread(client=asker, title="Drum makes noise when spinning", **fields):
    return client.post(f"{API}/threads", json={"title": title, "category": "washing-machine", **fields})


def ask_about_pump(client=asker, title="Does this include the rubber O-ring seal?"):
    return client.post(f"{API}/threads", json={"title": title, "product_type": "part", "product_id": "pump-1"})


def reply(thread_id, client=helper, body="Check the shock absorbers first."):
    return client.post(f"{API}/threads/{thread_id}/replies", json={"body": body})


# categories

def test_the_category_list_is_fixed():
    categories = asker.get(f"{API}/categories").json()["categories"]

    assert {"id": "washing-machine", "name": "Washing machines"} in categories
    assert categories[-1]["id"] == "other"


def test_unknown_categories_are_refused(forum, store):
    assert asker.get(f"{API}/categories/cars/threads").status_code == 404
    assert new_thread(category="cars").status_code == 404
    assert asker.post(f"{API}/threads", json={"title": "No category at all here"}).status_code == 422


# threads

def test_create_a_thread_and_see_it_after_refresh(forum, store):
    created = new_thread(body="  Bosch  Serie 4,\n\n\n\nsince last week.  ", appliance_model="Bosch WAN28209GB")
    assert created.status_code == 201
    thread = created.json()
    assert thread["author_name"] == "Davide P."
    assert thread["body"] == "Bosch Serie 4,\n\nsince last week."
    assert thread["category"] == "washing-machine" and thread["product"] is None
    assert thread["is_mine"] is True and thread["reply_count"] == 0
    assert "uid" not in thread

    listed = helper.get(WASHERS).json()
    assert [t["thread_id"] for t in listed["threads"]] == [thread["thread_id"]]
    assert listed["threads"][0]["is_mine"] is False
    assert helper.get(f"{API}/threads/{thread['thread_id']}").json()["appliance_model"] == "Bosch WAN28209GB"


def test_a_thread_about_a_product_takes_the_product_category(forum, store):
    thread = ask_about_pump().json()

    assert thread["category"] == "washing-machine"
    assert thread["product"] == {"product_type": "part", "product_id": "pump-1", "title": "Drain pump"}
    assert thread["appliance_model"] == "Bosch WAN28209GB"
    assert [t["thread_id"] for t in asker.get(WASHERS).json()["threads"]] == [thread["thread_id"]]
    assert asker.get(PUMP).json()["total"] == 1


def test_a_product_thread_needs_an_existing_active_product(forum, store):
    def ask(product_type, product_id):
        return asker.post(f"{API}/threads", json={"title": "Is it still available?",
                                                   "product_type": product_type, "product_id": product_id})

    assert ask("part", "missing").status_code == 404
    assert ask("used", "sold-tv").status_code == 409
    assert asker.post(f"{API}/threads", json={"title": "Half a product id", "product_id": "pump-1"}).status_code == 422


def test_an_unknown_product_category_falls_back_to_other(forum, store):
    store.items[("used", "radio-1")] = {"title": "Radio", "category": "audio", "status": "active",
                                        "owner_uid": SELLER, "price_cents": 1000}

    thread = asker.post(f"{API}/threads", json={"title": "Does the FM band work?",
                                                 "product_type": "used", "product_id": "radio-1"}).json()
    assert thread["category"] == "other"


def test_category_threads_sorted_by_activity_or_votes(forum, store):
    old = new_thread(title="Old thread with many votes").json()["thread_id"]
    new = new_thread(title="New thread without any vote").json()["thread_id"]
    helper.put(f"{API}/threads/{old}/vote")

    assert [t["thread_id"] for t in asker.get(WASHERS).json()["threads"]] == [new, old]
    assert [t["thread_id"] for t in asker.get(WASHERS, params={"sort": "popular"}).json()["threads"]] == [old, new]

    reply(old)  # a reply moves the thread back to the top of "recent"
    assert asker.get(WASHERS).json()["threads"][0]["thread_id"] == old
    assert asker.get(WASHERS, params={"sort": "random"}).status_code == 422


def test_category_threads_are_paginated(forum, store):
    ids = [new_thread(client=client_for(f"user-{n}"), title=f"Thread number {n} here").json()["thread_id"]
           for n in range(5)]

    first = asker.get(WASHERS, params={"limit": 2}).json()
    second = asker.get(WASHERS, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    third = asker.get(WASHERS, params={"limit": 2, "cursor": second["next_cursor"]}).json()

    seen = [t["thread_id"] for page in (first, second, third) for t in page["threads"]]
    assert seen == list(reversed(ids))
    assert third["next_cursor"] is None
    assert asker.get(WASHERS, params={"cursor": "nope"}).status_code == 422
    assert asker.get(WASHERS, params={"limit": 0}).status_code == 422


# replies

def test_replies_oldest_first_with_pagination(forum, store):
    thread_id = new_thread().json()["thread_id"]
    bodies = [f"Reply number {n}" for n in range(5)]
    for body in bodies:
        reply(thread_id, body=body)

    first = asker.get(f"{API}/threads/{thread_id}/replies", params={"limit": 3}).json()
    rest = asker.get(f"{API}/threads/{thread_id}/replies", params={"limit": 3, "cursor": first["next_cursor"]}).json()

    assert [r["body"] for r in first["replies"] + rest["replies"]] == bodies
    assert rest["next_cursor"] is None
    assert first["replies"][0]["author_name"] == "Anna" and first["replies"][0]["is_mine"] is False
    assert asker.get(f"{API}/threads/{thread_id}").json()["reply_count"] == 5


def test_the_product_page_shows_the_best_reply(forum, store):
    thread_id = ask_about_pump().json()["thread_id"]
    reply(thread_id, body="First reply")
    best = reply(thread_id, client=seller, body="Better reply").json()["reply_id"]
    asker.put(f"{API}/threads/{thread_id}/replies/{best}/vote")

    thread = asker.get(PUMP).json()["threads"][0]
    assert thread["top_reply"]["body"] == "Better reply"
    assert thread["top_reply"]["voted_by_me"] is True
    assert asker.get(PUMP, params={"sort": "recent"}).json()["total"] == 1


def test_a_product_without_threads_is_empty(forum, store):
    assert asker.get(PUMP).json() == {"threads": [], "total": 0}


def test_badges_are_decided_by_the_server(forum, store):
    listing = asker.post(f"{API}/threads", json={"title": "Does the ice maker still work?",
                                                  "product_type": "used", "product_id": "fridge-2"}).json()
    assert reply(listing["thread_id"], client=seller).json()["author_badge"] == "seller"

    store.orders["o1"] = {"uid": HELPER, "status": "paid", "created_at": 1,
                          "items": [{"product_type": "part", "product_id": "pump-1", "quantity": 1}]}
    store.orders["o2"] = {"uid": ASKER, "status": "pending", "created_at": 1,
                          "items": [{"product_type": "part", "product_id": "pump-1", "quantity": 1}]}
    pump = ask_about_pump().json()
    assert pump["author_badge"] is None  # an unpaid order does not count
    assert reply(pump["thread_id"]).json()["author_badge"] == "verified_buyer"
    assert reply(new_thread().json()["thread_id"]).json()["author_badge"] is None  # no product, no badge


# validation

@pytest.mark.parametrize(
    "title, body, detail",
    [
        ("Short?", None, "the title must be at least 8 characters"),
        ("        ​​        ", None, "the title must be at least 8 characters"),
        ("A" * 151, None, "the title can be at most 150 characters"),
        ("Is the size right for me?", "x" * 2001, "the text can be at most 2000 characters"),
    ],
)
def test_text_is_validated_after_trimming(forum, store, title, body, detail):
    result = new_thread(title=title, body=body)

    assert result.status_code == 422
    assert result.json()["detail"] == detail
    assert forum.threads == {}


def test_control_characters_are_removed_and_names_are_safe(forum, store):
    thread = new_thread(client=client_for("u-x", "  Eve\u0007  ‮Admin  "), title="Is it ‮loud\u0007 now?").json()

    assert thread["title"] == "Is it loud now?"
    assert thread["author_name"] == "Eve Admin"
    assert new_thread(client=client_for("no-name")).json()["author_name"] == forum_service.DEFAULT_AUTHOR


def test_a_reply_must_not_be_empty(forum, store):
    thread_id = new_thread().json()["thread_id"]

    assert reply(thread_id, body=" ").json()["detail"] == "the reply must be at least 2 characters"


# edit and delete

def test_only_the_author_can_edit(forum, store):
    thread_id = new_thread().json()["thread_id"]
    reply_id = reply(thread_id).json()["reply_id"]

    edited = asker.patch(f"{API}/threads/{thread_id}", json={"title": "Drum is loud when spinning fast"}).json()
    assert edited["title"] == "Drum is loud when spinning fast" and edited["edited_at"]
    assert helper.patch(f"{API}/threads/{thread_id}", json={"title": "Hijacked thread title"}).status_code == 404
    assert asker.patch(f"{API}/threads/{thread_id}", json={}).status_code == 422

    url = f"{API}/threads/{thread_id}/replies/{reply_id}"
    assert helper.patch(url, json={"body": "Updated reply"}).json()["body"] == "Updated reply"
    assert asker.patch(url, json={"body": "Not mine"}).status_code == 404


def test_only_the_author_can_delete(forum, store):
    thread_id = new_thread().json()["thread_id"]
    reply_id = reply(thread_id).json()["reply_id"]
    reply_url = f"{API}/threads/{thread_id}/replies/{reply_id}"

    assert asker.delete(reply_url).status_code == 404
    assert helper.delete(reply_url).status_code == 204
    assert asker.get(f"{API}/threads/{thread_id}").json()["reply_count"] == 0
    assert asker.get(f"{API}/threads/{thread_id}/replies").json()["replies"] == []

    assert helper.delete(f"{API}/threads/{thread_id}").status_code == 404
    assert asker.delete(f"{API}/threads/{thread_id}").status_code == 204
    assert asker.get(f"{API}/threads/{thread_id}").status_code == 404
    assert asker.get(WASHERS).json()["threads"] == []


# votes

def test_votes_are_one_per_user_and_can_be_withdrawn(forum, store):
    thread_id = new_thread().json()["thread_id"]
    vote = f"{API}/threads/{thread_id}/vote"

    assert helper.put(vote).json() == {"upvotes": 1, "voted_by_me": True}
    assert helper.put(vote).json() == {"upvotes": 1, "voted_by_me": True}
    assert seller.put(vote).json()["upvotes"] == 2
    assert helper.get(WASHERS).json()["threads"][0]["voted_by_me"] is True

    assert helper.delete(vote).json() == {"upvotes": 1, "voted_by_me": False}
    assert helper.delete(vote).json() == {"upvotes": 1, "voted_by_me": False}
    assert asker.put(vote).status_code == 409  # own thread


# reports and moderation

def test_reports_are_stored_and_three_hide_a_post(forum, store):
    thread_id = new_thread().json()["thread_id"]
    report = f"{API}/threads/{thread_id}/report"

    for client in (helper, seller):
        assert client.post(report, json={"reason": "spam"}).status_code == 202
    assert helper.post(report, json={"reason": "spam"}).status_code == 202  # repeated: not counted twice
    assert len(forum.reports) == 2 and len(asker.get(WASHERS).json()["threads"]) == 1

    client_for("third").post(report, json={"reason": "unsafe", "note": "says to open it while plugged in"})
    assert forum.threads[thread_id]["status"] == "hidden"
    assert asker.get(WASHERS).json()["threads"] == []
    assert any(r["note"] == "says to open it while plugged in" for r in forum.reports.values())


def test_a_hidden_reply_leaves_the_thread(forum, store):
    thread_id = new_thread().json()["thread_id"]
    reply_id = reply(thread_id, client=seller).json()["reply_id"]
    for uid in ("r1", "r2", "r3"):
        client_for(uid).post(f"{API}/threads/{thread_id}/replies/{reply_id}/report", json={"reason": "offensive"})

    assert asker.get(f"{API}/threads/{thread_id}").json()["reply_count"] == 0
    assert asker.get(f"{API}/threads/{thread_id}/replies").json()["replies"] == []


def test_own_posts_cannot_be_reported_and_reasons_are_checked(forum, store):
    thread_id = new_thread().json()["thread_id"]
    report = f"{API}/threads/{thread_id}/report"

    assert asker.post(report, json={"reason": "spam"}).status_code == 409
    assert helper.post(report, json={"reason": "boring"}).status_code == 422


def test_an_admin_can_hide_and_restore(forum, store):
    thread_id = new_thread().json()["thread_id"]
    reply_id = reply(thread_id).json()["reply_id"]
    moderation = f"{API}/threads/{thread_id}/replies/{reply_id}/moderation"

    assert asker.post(moderation, json={"action": "hide"}).status_code == 403  # not an admin
    assert admin.post(moderation, json={"action": "hide"}).json() == {"status": "hidden"}
    assert asker.get(f"{API}/threads/{thread_id}").json()["reply_count"] == 0
    assert admin.post(moderation, json={"action": "hide"}).json() == {"status": "hidden"}  # no double count

    admin.post(moderation, json={"action": "restore"})
    assert asker.get(f"{API}/threads/{thread_id}").json()["reply_count"] == 1

    assert admin.post(f"{API}/threads/{thread_id}/moderation", json={"action": "hide"}).json()["status"] == "hidden"
    assert asker.get(f"{API}/threads/{thread_id}").status_code == 404
    assert admin.post(f"{API}/threads/missing/moderation", json={"action": "hide"}).status_code == 404


# errors and limits

def test_unknown_ids_are_404_and_bad_ids_422(forum, store):
    assert asker.get(f"{API}/threads/nope").status_code == 404
    assert reply("nope").status_code == 404
    assert helper.put(f"{API}/threads/t1/replies/nope/vote").status_code == 404
    assert asker.get(f"{API}/threads/bad.id").status_code == 422


def test_post_creation_is_rate_limited_per_user(forum, store):
    for n in range(10):
        assert new_thread(title=f"Spam thread number {n}").status_code == 201

    assert new_thread().status_code == 429
    assert new_thread(client=helper).status_code == 201  # another user has their own budget
    assert asker.get(WASHERS).status_code == 200  # reading is not limited


def test_the_forum_needs_a_token(forum, store):
    assert TestClient(app).get(WASHERS).status_code == 401


def test_a_misconfigured_database_answers_503(monkeypatch, store):
    import firebase_store

    def broken():
        raise firebase_store.FirebaseConfigurationError("no credentials")

    monkeypatch.setattr(forum_service, "get_store", broken)

    result = asker.get(WASHERS)
    assert result.status_code == 503 and result.json()["detail"] == "the forum is unavailable"
