"""From the diagnosis to a spare part of the catalogue (#30)."""

from __future__ import annotations

import firebase_store
import part_suggestions as ps
import pytest
from fastapi.testclient import TestClient
from index import app
from jwt_helpers import bearer, make_token

PARTS = [
    {"id": "HDW-P-001", "type": "part", "title": "Drain pump for Bosch WAN28201GB", "brand": "Bosch",
     "model": "WAN28201GB", "category": "washing machine", "price": 24.9, "image_urls": ["https://x.test/p1.jpg"]},
    {"id": "HDW-P-010", "type": "part", "title": "Drain pump for Samsung WW90T534DAW", "brand": "Samsung",
     "model": "WW90T534DAW", "category": "washing machine", "price": 29.9, "image_urls": []},
    {"id": "HDW-P-020", "type": "part", "title": "Drain pump for Beko DFN05310W", "brand": "Beko",
     "model": "DFN05310W", "category": "dishwasher", "price": 19.9, "image_urls": []},
    {"id": "HDW-P-030", "type": "part", "title": "Door lock interlock for Bosch WAN28201GB", "brand": "Bosch",
     "model": "WAN28201GB", "category": "washing machine", "price": 19.9, "image_urls": []},
]


def parts_for(component):
    return [part for part in PARTS if part["title"].lower().startswith(component.lower())]


# ---------- the curated table ----------

@pytest.mark.parametrize(
    ("text", "brand", "component"),
    [
        ("My washing machine shows E18", "Bosch", ps.DRAIN_PUMP),
        ("error F 18 after the wash", "Siemens", ps.DRAIN_PUMP),
        ("The display shows 5E", "Samsung", ps.DRAIN_PUMP),
        ("error code dE on the screen", "LG", ps.DOOR_LOCK),
        ("It shows HE1", "Samsung", ps.HEATING_ELEMENT),
        ("E40 keeps flashing", "AEG", ps.DOOR_LOCK),
        ("Code F9 E1", "Whirlpool", ps.DRAIN_PUMP),
    ],
)
def test_known_error_codes_point_to_their_part(text, brand, component):
    diagnosis = ps.diagnose(text, brand=brand)

    assert diagnosis.component == component
    assert diagnosis.confidence == 0.9


def test_a_code_of_one_brand_only_works_without_the_brand():
    diagnosis = ps.diagnose("It stopped with E20")

    assert diagnosis.component == ps.DRAIN_PUMP
    assert diagnosis.confidence == 0.8


def test_a_code_that_means_different_things_needs_the_brand():
    # E01 is Candy's door lock but not known for other brands: still unique.
    assert ps.diagnose("error E01").component == ps.DOOR_LOCK
    # An unknown code for the brand is no diagnosis.
    assert ps.diagnose("error E99", brand="Bosch") is None


def test_ordinary_words_are_not_error_codes():
    # "de" and "he" are words, not Samsung's DE / HE codes.
    assert ps.diagnose("error de la lavadora, he dicho") is None


@pytest.mark.parametrize(
    ("text", "category", "component"),
    [
        ("The washing machine won't drain", None, ps.DRAIN_PUMP),
        ("door won't open after the cycle", ps.WASHER, ps.DOOR_LOCK),
        ("the dishwasher doesn't heat the water", None, ps.HEATING_ELEMENT),
        ("the drum doesn't spin but I hear the motor", ps.WASHER, ps.DRUM_BELT),
        ("my fridge is too warm", None, ps.THERMOSTAT),
        ("the oven light is not working", None, ps.OVEN_LAMP),
    ],
)
def test_symptoms_point_to_their_part(text, category, component):
    category = category or ps.category_of(text)

    diagnosis = ps.diagnose(text, category=category)

    assert diagnosis.component == component
    assert diagnosis.confidence == 0.75


def test_a_symptom_of_another_appliance_is_ignored():
    assert ps.diagnose("door won't open", category=ps.OVEN) is None


def test_nothing_known_is_no_diagnosis():
    assert ps.diagnose("How do I set the clock?") is None


# ---------- the part of the catalogue ----------

def test_the_exact_model_wins_and_fits():
    part = ps.suggest("shows E18", brand="Bosch", model="WAN 28201GB", parts_for=parts_for)

    assert part["id"] == "HDW-P-001"
    assert part["compatible"] is True
    assert part["confidence"] == 0.95
    assert part["price_cents"] == 2490
    assert part["image_url"] == "https://x.test/p1.jpg"
    assert part["name"] == "Drain pump for Bosch WAN28201GB"


def test_another_model_is_offered_as_not_fitting_with_less_confidence():
    part = ps.suggest("shows 5E", brand="Samsung", model="WW80", parts_for=parts_for)

    assert part["id"] == "HDW-P-010"
    assert part["compatible"] is False
    assert part["confidence"] == 0.8


def test_without_the_model_compatibility_is_unknown():
    part = ps.suggest("The washing machine won't drain", parts_for=parts_for)

    assert part["compatible"] is None
    assert part["confidence"] == 0.75
    assert part["id"] in {"HDW-P-001", "HDW-P-010"}


def test_a_symptom_with_a_part_that_does_not_fit_stays_under_the_threshold():
    assert ps.suggest("The washing machine won't drain", model="XYZ", parts_for=parts_for) is None


def test_only_parts_of_the_same_appliance_type():
    part = ps.suggest("dishwasher not draining", parts_for=parts_for)
    assert part["id"] == "HDW-P-020"
    # No oven lamp in the catalogue: nothing to offer.
    assert ps.suggest("the oven light is not working", parts_for=parts_for) is None


def test_uncurated_problems_return_none():
    assert ps.suggest("How do I set the clock on my oven?", parts_for=parts_for) is None


def test_the_catalogue_is_searched_for_parts_of_that_component(monkeypatch):
    seen = {}

    def fake_search(**kwargs):
        seen.update(kwargs)
        return {"items": PARTS + [{"id": "L1", "type": "used", "title": "Drain pump used"}]}

    monkeypatch.setattr(firebase_store, "search_marketplace", fake_search)

    parts = ps._catalogue_parts(ps.DRAIN_PUMP)

    assert seen["query"] == ps.DRAIN_PUMP and seen["item_type"] == "part"
    assert [p["id"] for p in parts] == ["HDW-P-001", "HDW-P-010", "HDW-P-020"]


# ---------- in the chat answer ----------

def test_a_manual_answer_gets_the_suggested_part(monkeypatch):
    monkeypatch.setattr(ps, "_catalogue_parts", parts_for)
    answer = {"reply": "Check the pump filter.", "is_manual": True, "appliance_name": "Bosch Washing Machine",
              "issue_title": "E18 error", "brand": "Bosch", "model": "WAN28201GB", "steps": [{"number": 1}]}

    result = ps.attach_to_response(answer, "My washing machine shows E18")

    assert result["suggested_part"]["id"] == "HDW-P-001"
    # The rest of the JSON contract is unchanged.
    assert {k: v for k, v in result.items() if k != "suggested_part"} == answer


def test_an_answer_without_a_diagnosis_has_a_null_part(monkeypatch):
    monkeypatch.setattr(ps, "_catalogue_parts", parts_for)
    answer = {"reply": "Set the clock", "is_manual": True, "appliance_name": "Oven", "issue_title": "Clock"}

    assert ps.attach_to_response(answer, "How do I set the clock?")["suggested_part"] is None


def test_a_plain_answer_is_left_as_it_is():
    answer = {"reply": "Hello", "image": None}

    assert ps.attach_to_response(answer, "hi") is answer


def test_a_catalogue_failure_never_breaks_the_answer(monkeypatch):
    def broken(component):
        raise RuntimeError("Firestore down")

    monkeypatch.setattr(ps, "_catalogue_parts", broken)
    answer = {"reply": "x", "is_manual": True, "appliance_name": "Washer", "issue_title": "E18", "brand": "Bosch"}

    assert ps.attach_to_response(answer, "E18")["suggested_part"] is None


# ---------- the endpoint ----------

client = TestClient(app, headers=bearer(make_token(sub="user-1")))


def test_the_endpoint_suggests_a_part(monkeypatch):
    monkeypatch.setattr(ps, "_catalogue_parts", parts_for)

    response = client.get("/api/marketplace/parts/suggest",
                          params={"error_code": "E18", "brand": "Bosch", "model": "WAN28201GB"})

    assert response.status_code == 200
    assert response.json()["suggested_part"]["id"] == "HDW-P-001"


def test_the_endpoint_returns_null_for_unknown_codes(monkeypatch):
    monkeypatch.setattr(ps, "_catalogue_parts", parts_for)

    response = client.get("/api/marketplace/parts/suggest", params={"error_code": "Z99", "brand": "Bosch"})

    assert response.json() == {"suggested_part": None}


def test_the_endpoint_reads_a_symptom(monkeypatch):
    monkeypatch.setattr(ps, "_catalogue_parts", parts_for)

    response = client.get("/api/marketplace/parts/suggest",
                          params={"symptom": "door won't open", "category": "washing machine", "brand": "Bosch",
                                  "model": "WAN28201GB"})

    assert response.json()["suggested_part"]["id"] == "HDW-P-030"


def test_the_endpoint_needs_a_code_or_a_symptom():
    assert client.get("/api/marketplace/parts/suggest").status_code == 422


def test_the_endpoint_reports_an_unavailable_catalogue(monkeypatch):
    def broken(component):
        raise firebase_store.FirebaseConfigurationError("no credentials")

    monkeypatch.setattr(ps, "_catalogue_parts", broken)

    response = client.get("/api/marketplace/parts/suggest", params={"error_code": "E18", "brand": "Bosch"})

    assert response.status_code == 503
