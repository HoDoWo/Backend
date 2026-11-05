"""Firestore side of the product forum: ids, counters and one-vote-per-user."""

from __future__ import annotations

import pytest
from google.api_core import exceptions as google_exceptions

import firebase_store
import forum_store


class Inc:
    def __init__(self, amount):
        self.amount = amount


def apply(doc, fields):
    for key, value in fields.items():
        doc[key] = doc.get(key, 0) + value.amount if isinstance(value, Inc) else value


class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data, self.exists = doc_id, data, data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class Doc:
    def __init__(self, db, path):
        self.db, self.path, self.id = db, path, path.rsplit("/", 1)[-1]

    def get(self):
        return Snap(self.id, self.db.docs.get(self.path))

    def set(self, data):
        self.db.docs[self.path] = dict(data)

    def create(self, data):
        if self.path in self.db.docs:
            raise google_exceptions.AlreadyExists("exists")
        self.set(data)

    def update(self, fields):
        apply(self.db.docs[self.path], fields)

    def delete(self):
        self.db.docs.pop(self.path, None)

    def collection(self, name):
        return Collection(self.db, f"{self.path}/{name}")


class Collection:
    def __init__(self, db, path, filters=(), orders=(), after=None, limit=None):
        self.db, self.path, self.filters, self.orders = db, path, filters, orders
        self.after, self._limit = after, limit

    def _copy(self, **changes):
        fields = {"filters": self.filters, "orders": self.orders, "after": self.after, "limit": self._limit}
        fields.update(changes)
        return Collection(self.db, self.path, **fields)

    def document(self, doc_id=None):
        if doc_id is None:
            self.db.counter += 1
            doc_id = f"auto{self.db.counter}"
        return Doc(self.db, f"{self.path}/{doc_id}")

    def where(self, field, op, value):
        assert op == "=="
        return self._copy(filters=self.filters + ((field, value),))

    def order_by(self, field, direction="ASCENDING"):
        return self._copy(orders=self.orders + ((field, direction == "DESCENDING"),))

    def start_after(self, snapshot):
        return self._copy(after=snapshot.id)

    def limit(self, count):
        return self._copy(limit=count)

    def stream(self):
        prefix = self.path + "/"
        found = [
            Snap(path[len(prefix):], data) for path, data in self.db.docs.items()
            if path.startswith(prefix) and "/" not in path[len(prefix):]
            and all(data.get(f) == v for f, v in self.filters)
        ]
        for field, descending in reversed(self.orders):
            found.sort(key=lambda snap: snap.to_dict()[field], reverse=descending)
        if self.after is not None:
            ids = [snap.id for snap in found]
            found = found[ids.index(self.after) + 1:]
        return found[: self._limit]


class Batch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def set(self, ref, data):
        self.ops.append(lambda: ref.set(data))

    def update(self, ref, fields):
        self.ops.append(lambda: ref.update(fields))

    def delete(self, ref):
        self.ops.append(ref.delete)

    def commit(self):
        self.db.commits += 1
        for op in self.ops:
            op()


class Db:
    def __init__(self):
        self.docs, self.counter, self.commits = {}, 0, 0

    def collection(self, name):
        return Collection(self, name)

    def batch(self):
        return Batch(self)

    def get_all(self, refs):
        return [ref.get() for ref in refs]


@pytest.fixture
def db(monkeypatch):
    fake = Db()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: fake)
    monkeypatch.setattr(forum_store, "_increment", Inc)
    monkeypatch.setattr(forum_store, "_descending", lambda: "DESCENDING")
    return fake


def new_thread(product_key="part:pump-1", category="washing-machine", activity=1, upvotes=0, status="visible"):
    return forum_store.create_thread({"product_key": product_key, "category": category, "title": "Q?",
                                      "upvotes": upvotes, "reply_count": 0, "report_count": 0,
                                      "status": status, "last_activity_at": activity})


def test_product_threads_are_listed_by_product(db):
    first = new_thread()
    new_thread("part:other")

    assert [t["thread_id"] for t in forum_store.list_product_threads("part:pump-1")] == [first]
    assert forum_store.get_thread(first)["title"] == "Q?"
    assert forum_store.get_thread("missing") is None


def test_category_threads_are_ordered_and_paged(db):
    old = new_thread(activity=1, upvotes=5)
    mid = new_thread(activity=2)
    new = new_thread(activity=3)
    new_thread(activity=4, status="hidden")
    new_thread(activity=5, category="oven")

    page, more = forum_store.list_category_threads("washing-machine", "recent", 2, None)
    assert [t["thread_id"] for t in page] == [new, mid] and more is True
    page, more = forum_store.list_category_threads("washing-machine", "recent", 2, mid)
    assert [t["thread_id"] for t in page] == [old] and more is False

    page, _ = forum_store.list_category_threads("washing-machine", "popular", 5, None)
    assert [t["thread_id"] for t in page] == [old, new, mid]

    with pytest.raises(forum_store.InvalidCursor):
        forum_store.list_category_threads("washing-machine", "recent", 2, "missing")


def test_a_reply_and_its_counter_are_written_in_one_batch(db):
    thread_id = new_thread()

    first = forum_store.create_reply(thread_id, {"body": "A", "created_at": 5, "upvotes": 0,
                                                 "report_count": 0, "status": "visible"})
    second = forum_store.create_reply(thread_id, {"body": "B", "created_at": 6, "upvotes": 0,
                                                  "report_count": 0, "status": "visible"})

    assert db.commits == 2
    assert forum_store.get_thread(thread_id)["reply_count"] == 2
    assert forum_store.get_thread(thread_id)["last_activity_at"] == 6
    page, more = forum_store.list_replies(thread_id, 1, None)
    assert [r["reply_id"] for r in page] == [first] and more is True
    assert [r["reply_id"] for r in forum_store.list_replies(thread_id, 1, first)[0]] == [second]
    assert {r["reply_id"] for r in forum_store.all_replies(thread_id)} == {first, second}
    assert forum_store.get_reply(thread_id, first)["body"] == "A"
    assert forum_store.get_reply(thread_id, "missing") is None

    forum_store.update_post(thread_id, first, {"body": "A2"})
    assert forum_store.get_reply(thread_id, first)["body"] == "A2"

    forum_store.set_status(thread_id, first, "deleted", -1)
    assert forum_store.get_thread(thread_id)["reply_count"] == 1
    assert [r["reply_id"] for r in forum_store.list_replies(thread_id, 5, None)[0]] == [second]


def test_a_vote_counts_once_and_can_be_withdrawn(db):
    thread_id = new_thread()
    vote_id = "u1_" + thread_id

    assert forum_store.add_vote(vote_id, {"uid": "u1"}, thread_id, None) is True
    assert forum_store.add_vote(vote_id, {"uid": "u1"}, thread_id, None) is False
    assert forum_store.get_thread(thread_id)["upvotes"] == 1
    assert forum_store.voted([vote_id, "u2_" + thread_id]) == {vote_id}
    assert forum_store.voted([]) == set()

    assert forum_store.remove_vote(vote_id, thread_id, None) is True
    assert forum_store.remove_vote(vote_id, thread_id, None) is False
    assert forum_store.get_thread(thread_id)["upvotes"] == 0


def test_a_report_counts_once_per_user(db):
    thread_id = new_thread()

    assert forum_store.add_report("u1_" + thread_id, {"reason": "spam"}, thread_id, None) is True
    assert forum_store.add_report("u1_" + thread_id, {"reason": "spam"}, thread_id, None) is False
    assert forum_store.get_thread(thread_id)["report_count"] == 1
