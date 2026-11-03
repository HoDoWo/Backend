"""Partner feed import (#24) and curated seed (#33). No network, no Firestore."""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib

import pytest

import catalogue_import
import checkout_service
import firebase_store

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "seed"

spec = importlib.util.spec_from_file_location("import_partner_feed", ROOT / "scripts" / "import_partner_feed.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class MemoryStore:
    """In-memory stand-in for FirestoreCatalogue (merge writes, like batch.set(merge=True))."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data or {})
        self.writes = 0

    def read(self, collection, field=None, value=None):
        docs = self.data.get(collection, {})
        return {k: copy.deepcopy(v) for k, v in docs.items() if field is None or v.get(field) == value}

    def write(self, collection, documents):
        self.writes += 1
        target = self.data.setdefault(collection, {})
        for doc_id, fields in documents.items():
            target.setdefault(doc_id, {}).update(copy.deepcopy(fields))


PARTNERS = {"partners": {"voltacasa": {"name": "VoltaCasa", "partner_tier": 3}}}
HEADER = "sku,title,brand,model,category,price,stock,sponsored\n"


def feed(tmp_path, body, name="voltacasa.csv"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_csv_feed_is_normalised_into_partner_products(tmp_path):
    store = MemoryStore(PARTNERS)
    path = feed(tmp_path, HEADER + "W-1,  Aurora Washer 8kg ,Aurora,AW-1,Washing Machine,\"399,90\",5,yes\n")

    report = catalogue_import.import_partner_feed(store, "VoltaCasa", path, dry_run=False)

    assert report.summary().endswith("1 new, 0 updated, 0 unchanged, 0 rejected")
    doc = store.data["partner_products"]["voltacasa_W-1"]
    assert doc["title"] == "Aurora Washer 8kg"
    assert doc["title_normalised"] == "aurora washer 8kg"
    assert doc["category"] == "washing machine"
    assert doc["price_cents"] == 39990
    assert doc["stock"] == 5 and doc["sponsored"] is True
    assert doc["partner_tier"] == 3 and doc["partner_name"] == "VoltaCasa"
    assert doc["item_type"] == "new" and doc["condition"] == "new"
    assert isinstance(doc["created_at"], int)


def test_json_feed_is_accepted(tmp_path):
    store = MemoryStore(PARTNERS)
    path = feed(tmp_path, json.dumps({"partner_id": "voltacasa", "products": [
        {"sku": "O-1", "title": "Kelvo Oven", "brand": "Kelvo", "model": "KO-1", "category": "oven",
         "price_cents": 41900, "stock": 2}]}), "voltacasa.json")

    report = catalogue_import.import_partner_feed(store, "voltacasa", path, dry_run=False)

    assert report.new == ["voltacasa_O-1"]
    assert store.data["partner_products"]["voltacasa_O-1"]["price_cents"] == 41900


def test_running_twice_does_not_duplicate_and_keeps_created_at(tmp_path):
    store = MemoryStore(PARTNERS)
    path = feed(tmp_path, HEADER + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n")
    catalogue_import.import_partner_feed(store, "voltacasa", path, dry_run=False)
    created = store.data["partner_products"]["voltacasa_W-1"]["created_at"]

    second = catalogue_import.import_partner_feed(store, "voltacasa", path, dry_run=False)

    assert len(store.data["partner_products"]) == 1
    assert second.unchanged == 1 and not second.new and not second.updated
    assert store.data["partner_products"]["voltacasa_W-1"]["created_at"] == created


def test_changes_are_reported_field_by_field(tmp_path):
    store = MemoryStore(PARTNERS)
    catalogue_import.import_partner_feed(
        store, "voltacasa", feed(tmp_path, HEADER + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n"),
        dry_run=False)

    report = catalogue_import.import_partner_feed(
        store, "voltacasa", feed(tmp_path, HEADER + "W-1,Aurora Washer,Aurora,AW-1,washing machine,379.00,3,false\n"),
        dry_run=False)

    assert report.updated == {"voltacasa_W-1": ["price_cents", "stock"]}
    assert "~ voltacasa_W-1: price_cents, stock" in "\n".join(report.details())


def test_dry_run_writes_nothing(tmp_path):
    store = MemoryStore(PARTNERS)
    path = feed(tmp_path, HEADER + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n")

    report = catalogue_import.import_partner_feed(store, "voltacasa", path, dry_run=True)

    assert report.new == ["voltacasa_W-1"]
    assert store.writes == 0 and "partner_products" not in store.data


def test_invalid_rows_are_skipped_with_line_and_reason(tmp_path):
    store = MemoryStore(PARTNERS)
    path = feed(tmp_path, HEADER
                + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n"
                + "W-2,,Aurora,AW-2,washing machine,10.00,1,false\n"
                + "W-3,Bad price,Aurora,AW-3,washing machine,abc,1,false\n"
                + "W-1,Duplicate,Aurora,AW-1,washing machine,1.00,1,false\n"
                + "W-4,Negative stock,Aurora,AW-4,washing machine,1.00,-2,false\n")

    report = catalogue_import.import_partner_feed(store, "voltacasa", path, dry_run=False)

    assert report.rejected == [
        "line 3: title is required",
        "line 4: price 'abc' is not a number",
        "line 5: duplicate of line 2 (voltacasa_W-1)",
        "line 6: stock must be between 0 and 100000",
    ]
    assert list(store.data["partner_products"]) == ["voltacasa_W-1"]


def test_products_missing_from_a_full_feed_go_out_of_stock_not_deleted(tmp_path):
    store = MemoryStore(PARTNERS)
    catalogue_import.import_partner_feed(store, "voltacasa", feed(tmp_path, HEADER
        + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n"
        + "W-2,Kelvo Washer,Kelvo,KW-2,washing machine,459.00,2,false\n"), dry_run=False)

    partial = catalogue_import.import_partner_feed(store, "voltacasa", feed(tmp_path, HEADER
        + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n"), dry_run=False)
    assert partial.delisted == [] and store.data["partner_products"]["voltacasa_W-2"]["stock"] == 2

    report = catalogue_import.import_partner_feed(store, "voltacasa", feed(tmp_path, HEADER
        + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n"), dry_run=False, full=True)

    assert report.delisted == ["voltacasa_W-2"]
    assert store.data["partner_products"]["voltacasa_W-2"]["stock"] == 0
    assert store.data["partner_products"]["voltacasa_W-2"]["title"] == "Kelvo Washer"  # still there


@pytest.mark.parametrize(
    "partner, name, body, error",
    [
        ("unknown", "x.csv", HEADER, "unknown partner 'unknown'"),
        ("voltacasa", "x.json", "{not json", "is not valid JSON"),
        ("voltacasa", "x.xml", "<feed/>", "only .csv and .json"),
    ],
)
def test_a_bad_feed_is_reported(tmp_path, partner, name, body, error):
    report = catalogue_import.import_partner_feed(MemoryStore(PARTNERS), partner, feed(tmp_path, body, name),
                                                 dry_run=False)

    assert report.failed and error in report.error


def test_a_bad_feed_does_not_stop_the_others(tmp_path, capsys):
    store = MemoryStore({"partners": {"voltacasa": {"name": "V", "partner_tier": 3},
                                      "nordhome": {"name": "N", "partner_tier": 2}}})
    good = feed(tmp_path, HEADER + "W-1,Aurora Washer,Aurora,AW-1,washing machine,399.00,5,false\n", "good.csv")

    code = cli.main(["--feed", f"voltacasa={tmp_path / 'missing.csv'}", "--feed", f"nordhome={good}"], store=store)

    assert code == 1
    assert "nordhome_W-1" in store.data["partner_products"]
    assert "FAILED" in capsys.readouterr().out


# --- curated seed (#33) -------------------------------------------------------


def test_seed_files_are_complete_and_valid():
    assert catalogue_import.check_seed(SEED) == []

    reports = catalogue_import.import_seed(MemoryStore(), SEED, dry_run=True)

    assert not any(r.failed or r.rejected for r in reports), [r.summary() for r in reports]


def test_seed_loads_expected_counts_and_is_idempotent(capsys):
    store = MemoryStore()

    assert cli.main(["--seed"], store=store) == 0
    first = {name: len(docs) for name, docs in store.data.items()}
    assert cli.main(["--seed"], store=store) == 0

    assert first == {name: len(docs) for name, docs in store.data.items()}
    assert first["partners"] >= 4 and first["partner_products"] >= 60 and first["parts"] >= 40
    assert len({(c["brand"], c["model"]) for c in store.data["part_compatibility"].values()}) >= 10
    assert len({p["partner_tier"] for p in store.data["partners"].values()}) >= 2
    assert "0 new, 0 updated" in capsys.readouterr().out.split("Import finished.")[-1]


def test_seed_covers_the_washer_used_in_the_app_tests():
    store = MemoryStore()
    catalogue_import.import_seed(store, SEED, dry_run=False)

    bosch = [c for c in store.data["part_compatibility"].values() if c["model"] == "WAN28201GB"]
    assert {c["part_id"] for c in bosch} <= set(store.data["parts"])
    assert len(bosch) >= 4


def test_incomplete_seed_exits_with_an_error(tmp_path, capsys):
    for name in ("partners.csv", "partner_products.csv", "parts.csv", "part_compatibility.csv"):
        lines = (SEED / name).read_text().splitlines()
        (tmp_path / name).write_text("\n".join(lines[:3]) + "\n")

    assert cli.main(["--seed", "--seed-dir", str(tmp_path), "--dry-run"], store=MemoryStore()) == 1
    assert "SEED INCOMPLETE" in capsys.readouterr().out


# --- the seeded catalogue works with search, cart and checkout ----------------


class Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data

    def to_dict(self):
        return dict(self._data)


class FakeClient:
    """Minimal Firestore client over a MemoryStore, for the real search code."""

    def __init__(self, store):
        self.store = store

    def collection(self, name):
        store = self.store

        class Coll:
            def stream(self):
                return [Snap(k, v) for k, v in store.data.get(name, {}).items()]

        return Coll()


def test_seeded_parts_are_found_by_search_and_buyable(monkeypatch):
    store = MemoryStore()
    catalogue_import.import_seed(store, SEED, dry_run=False)
    monkeypatch.setattr(firebase_store, "get_firestore_client", lambda: FakeClient(store))

    result = firebase_store.search_marketplace(
        query="drain pump", item_type="part", category=None, brand="Bosch",
        min_price=None, max_price=None, cursor=None, limit=20)

    assert result["items"], "the Bosch drain pump should be found"
    pump = result["items"][0]
    assert pump["type"] == "part"
    # The cart/checkout read the same document and accept it.
    item = {**store.data["parts"][pump["id"]], "seller_id": store.data["parts"][pump["id"]]["partner_id"] or "hodowo"}
    assert checkout_service.availability_problem("buyer-1", "part", item, 1) is None


def test_firestore_writes_are_merged_in_batches_of_at_most_450():
    commits = []

    class Batch:
        def __init__(self):
            self.ops = []

        def set(self, ref, data, merge=False):
            self.ops.append((ref, data, merge))

        def commit(self):
            commits.append(self.ops)

    class Client:
        def batch(self):
            return Batch()

        def collection(self, name):
            return type("C", (), {"document": lambda _self, doc_id: f"{name}/{doc_id}"})()

    catalogue_import.FirestoreCatalogue(Client()).write("parts", {f"p{i}": {"stock": i} for i in range(1000)})

    assert [len(ops) for ops in commits] == [450, 450, 100]
    assert commits[0][0] == ("parts/p0", {"stock": 0}, True)
