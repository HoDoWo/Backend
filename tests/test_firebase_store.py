from __future__ import annotations

import pytest

import firebase_store


class FakeSnapshot:
    def __init__(self, data=None):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class FakeDocument:
    def __init__(self, store, document_id):
        self.store = store
        self.id = document_id

    def set(self, data):
        self.store[self.id] = dict(data)

    def update(self, data):
        self.store[self.id].update(data)

    def get(self):
        return FakeSnapshot(self.store.get(self.id))

    def delete(self):
        self.store.pop(self.id, None)

    def collection(self, name):
        return FakeCollection(self.store.setdefault(f"{self.id}/{name}", {}))


class FakeCollection:
    def __init__(self, store):
        self.store = store
        self.next_id = 0

    def document(self, document_id=None):
        if document_id is None:
            self.next_id += 1
            document_id = f"manual-{self.next_id}"
        return FakeDocument(self.store, document_id)


class FakeClient:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store.setdefault(name, {}))


@pytest.fixture
def fake_firestore(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: client)
    monkeypatch.setattr(firebase_store, "firestore_server_timestamp", lambda: "timestamp")
    return client


def test_save_manual_persists_guide_fields(fake_firestore):
    manual_id = firebase_store.save_manual(
        "user-1",
        {
            "appliance_name": "Espresso machine",
            "issue_title": "Make coffee",
            "overview": "Prepare a coffee.",
            "steps": [{"number": 1, "title": "Fill", "description": "Add water."}],
            "warnings": ["Hot surface"],
            "image": "https://example.test/guide.png",
        },
    )

    saved = fake_firestore.store["manuals"][manual_id]
    assert saved["user_uid"] == "user-1"
    assert saved["image_url"] == "https://example.test/guide.png"
    assert saved["is_bookmarked"] is False


class FakeBlob:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.uploaded = None

    def upload_from_string(self, data, content_type=None):
        if self.fail:
            raise RuntimeError("bucket not configured")
        self.uploaded = (data, content_type)


def test_save_manual_uploads_the_photo(fake_firestore, monkeypatch):
    blobs = []

    def fake_blob(user_uid, manual_id):
        blobs.append(FakeBlob(f"manuals/{user_uid}/{manual_id}.jpg"))
        return blobs[-1]

    monkeypatch.setattr(firebase_store, "manual_image_blob", fake_blob)

    manual_id = firebase_store.save_manual("user-1", {"reply": "Guide", "image": "https://x/guide.png"}, b"jpg")

    saved = fake_firestore.store["manuals"][manual_id]
    assert saved["image_path"] == f"manuals/user-1/{manual_id}.jpg"
    assert saved["image_url"] == "https://x/guide.png"
    assert blobs[0].uploaded == (b"jpg", "image/jpeg")


def test_save_manual_keeps_the_manual_when_the_upload_fails(fake_firestore, monkeypatch):
    monkeypatch.setattr(
        firebase_store, "manual_image_blob", lambda uid, mid: FakeBlob("x", fail=True)
    )

    manual_id = firebase_store.save_manual("user-1", {"reply": "Guide"}, b"jpg")

    saved = fake_firestore.store["manuals"][manual_id]
    assert saved["overview"] == "Guide"
    assert saved["image_path"] is None


def test_save_manual_keeps_the_manual_when_storage_is_missing(fake_firestore, monkeypatch):
    def no_bucket(uid, mid):
        raise ValueError("Storage bucket name not specified")

    monkeypatch.setattr(firebase_store, "manual_image_blob", no_bucket)

    manual_id = firebase_store.save_manual("user-1", {"reply": "Guide"}, b"jpg")

    assert fake_firestore.store["manuals"][manual_id]["image_path"] is None


def test_bookmark_requires_owner_and_updates_state(fake_firestore):
    manual_id = firebase_store.save_manual("user-1", {"reply": "Guide"})

    assert firebase_store.update_bookmark("user-1", manual_id, True) is True
    assert fake_firestore.store["manuals"][manual_id]["is_bookmarked"] is True
    with pytest.raises(LookupError):
        firebase_store.update_bookmark("user-2", manual_id, False)


def test_rating_toggles_and_changes_for_owner(fake_firestore):
    manual_id = firebase_store.save_manual("user-1", {"reply": "Guide"})

    assert firebase_store.set_rating("user-1", manual_id, 1) == 1
    assert firebase_store.set_rating("user-1", manual_id, 1) == 0
    assert firebase_store.set_rating("user-1", manual_id, -1) == -1
    with pytest.raises(ValueError):
        firebase_store.set_rating("user-1", manual_id, 0)
