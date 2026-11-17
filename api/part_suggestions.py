"""From the AI diagnosis to a spare part of the catalogue (#30).

A curated table links well-known error codes and symptoms to the component
that usually fails (for example Bosch E18 → drain pump). The part offered is
the one of the catalogue (``parts`` collection) with that component in its
title, preferring the exact model, then the brand, then the appliance type.

The suggestion carries a ``confidence``: only suggestions at or above
``MIN_CONFIDENCE`` are returned, everything else is ``None``, so the app shows
the part only when the diagnosis really points to it.

    error code matched for the brand      0.90
    error code of one brand only          0.80 (brand not given)
    symptom described by the user         0.75
    the part fits the exact model         +0.05
    the model is known and it does not    -0.10
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import firebase_store
import logging_config

logger = logging_config.get_logger(__name__)

MIN_CONFIDENCE = 0.7

WASHER = "washing machine"
DISHWASHER = "dishwasher"
OVEN = "oven"
FRIDGE = "refrigerator"

DRAIN_PUMP = "Drain pump"
DOOR_LOCK = "Door lock interlock"
HEATING_ELEMENT = "Heating element"
DRUM_BELT = "Drum belt"
DOOR_SEAL = "Door seal"
OVEN_LAMP = "Oven lamp"
FAN_MOTOR = "Fan motor"
THERMOSTAT = "Thermostat"
SPRAY_ARM = "Spray arm"

# Brand families share their error codes.
BRAND_FAMILIES = {
    "bosch": "bosch", "siemens": "bosch", "neff": "bosch", "balay": "bosch",
    "samsung": "samsung",
    "lg": "lg",
    "electrolux": "electrolux", "aeg": "electrolux", "zanussi": "electrolux",
    "hotpoint": "hotpoint", "indesit": "hotpoint", "ariston": "hotpoint",
    "whirlpool": "whirlpool",
    "candy": "candy", "hoover": "candy",
}

# family -> code -> (component, appliance type). Codes are compared without spaces or dashes, in upper case.
ERROR_CODES: dict[str, dict[str, tuple[str, str]]] = {
    "bosch": {
        "E18": (DRAIN_PUMP, WASHER), "F18": (DRAIN_PUMP, WASHER),
        "F19": (HEATING_ELEMENT, WASHER),
        "E24": (DRAIN_PUMP, DISHWASHER), "E25": (DRAIN_PUMP, DISHWASHER),
        "E09": (HEATING_ELEMENT, DISHWASHER),
    },
    "samsung": {
        "5E": (DRAIN_PUMP, WASHER), "5C": (DRAIN_PUMP, WASHER), "SE": (DRAIN_PUMP, WASHER),
        "DE": (DOOR_LOCK, WASHER), "DC": (DOOR_LOCK, WASHER), "DE1": (DOOR_LOCK, WASHER),
        "DE2": (DOOR_LOCK, WASHER),
        "HE": (HEATING_ELEMENT, WASHER), "HE1": (HEATING_ELEMENT, WASHER), "HE2": (HEATING_ELEMENT, WASHER),
    },
    "lg": {
        "OE": (DRAIN_PUMP, WASHER),
        "DE": (DOOR_LOCK, WASHER), "DE1": (DOOR_LOCK, WASHER), "DE2": (DOOR_LOCK, WASHER),
    },
    "electrolux": {
        "E20": (DRAIN_PUMP, WASHER), "E21": (DRAIN_PUMP, WASHER),
        "E40": (DOOR_LOCK, WASHER), "E41": (DOOR_LOCK, WASHER),
        "E61": (HEATING_ELEMENT, WASHER), "E62": (HEATING_ELEMENT, WASHER),
    },
    "hotpoint": {
        "F05": (DRAIN_PUMP, WASHER),
        "F17": (DOOR_LOCK, WASHER),
    },
    "whirlpool": {
        "F9E1": (DRAIN_PUMP, WASHER),
    },
    "candy": {
        "E03": (DRAIN_PUMP, WASHER),
        "E01": (DOOR_LOCK, WASHER),
    },
}

_NOT = r"(?:won'?t|wont|doesn'?t|does not|didn'?t|is not|isn'?t|not|no longer|will not)"

# Symptoms the user describes: (pattern, component, appliance types it applies to).
SYMPTOMS: list[tuple[re.Pattern[str], str, tuple[str, ...]]] = [
    (re.compile(rf"{_NOT}\s+(?:\w+\s+)?drain|standing water|water (?:stays|is left|remains)", re.IGNORECASE),
     DRAIN_PUMP, (WASHER, DISHWASHER)),
    (re.compile(rf"door\s+{_NOT}\s+(?:open|unlock|lock|close)|door (?:is )?(?:stuck|locked)|door lock", re.IGNORECASE),
     DOOR_LOCK, (WASHER,)),
    (re.compile(rf"{_NOT}\s+(?:\w+\s+)?heat|{_NOT} get (?:hot|warm)|no hot water|water (?:is )?(?:cold|not warm)", re.IGNORECASE),
     HEATING_ELEMENT, (WASHER, DISHWASHER, OVEN)),
    (re.compile(rf"drum\s+{_NOT}\s+(?:spin|turn|rotate)|drive belt|drum belt", re.IGNORECASE),
     DRUM_BELT, (WASHER,)),
    (re.compile(r"leak\w*\s+(?:from|around|at|under)\s+(?:the\s+)?door|door (?:seal|gasket)", re.IGNORECASE),
     DOOR_SEAL, (WASHER, FRIDGE)),
    (re.compile(rf"(?:light|lamp|bulb)\s+(?:is\s+)?(?:out|off|broken|blown|{_NOT}\s+(?:work|turn on))", re.IGNORECASE),
     OVEN_LAMP, (OVEN,)),
    (re.compile(rf"fan\s+(?:is\s+)?(?:noisy|broken|{_NOT}\s+(?:work|turn|spin))", re.IGNORECASE),
     FAN_MOTOR, (OVEN, FRIDGE)),
    (re.compile(rf"{_NOT}\s+cool|too warm|thermostat", re.IGNORECASE),
     THERMOSTAT, (FRIDGE,)),
    (re.compile(rf"spray arm|dishes (?:are )?(?:still )?dirty|{_NOT}\s+clean(?:ing)?\s+(?:the\s+)?dishes", re.IGNORECASE),
     SPRAY_ARM, (DISHWASHER,)),
]

CATEGORY_WORDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"dish\s?washer", re.IGNORECASE), DISHWASHER),
    (re.compile(r"washing machine|washer|lavatrice|lavadora", re.IGNORECASE), WASHER),
    (re.compile(r"\boven\b|\bhorno\b|\bforno\b", re.IGNORECASE), OVEN),
    (re.compile(r"refrigerator|fridge|freezer|frigo", re.IGNORECASE), FRIDGE),
]

_CODE_TOKEN = re.compile(r"\b([A-Za-z]{1,2}\s?-?\d{1,3}(?:\s?[A-Za-z]\d)?|\d[A-Za-z]|[A-Za-z]{2}\d?)\b")


@dataclass(frozen=True)
class Diagnosis:
    """The failing component, the appliance type it belongs to, how sure we are and why."""

    component: str
    categories: tuple[str, ...]
    confidence: float
    reason: str


def _normalise_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code).upper()


def _family(brand: str | None) -> str | None:
    return BRAND_FAMILIES.get(str(brand or "").strip().lower())


def category_of(*texts: str | None) -> str | None:
    """The appliance type named in the texts (the first one found)."""
    for text in texts:
        for pattern, category in CATEGORY_WORDS:
            if text and pattern.search(text):
                return category
    return None


def _codes_in(text: str) -> list[str]:
    """Error codes written in the text. Two-letter codes ("OE", "dE") only next to "error"/"code"."""
    codes = []
    talks_about_codes = re.search(r"error|code|display|shows|showing|flashing", text, re.IGNORECASE)
    for match in _CODE_TOKEN.finditer(text):
        token = match.group(1)
        # A code without digits is written in capitals ("OE", "dE"), never an ordinary word ("de", "he").
        if not any(ch.isdigit() for ch in token) and (not talks_about_codes or not token[-1].isupper()):
            continue
        codes.append(_normalise_code(token))
    return codes


def diagnose(text: str, brand: str | None = None, error_code: str | None = None,
             category: str | None = None) -> Diagnosis | None:
    """The component the error code or the described symptom points to."""
    codes = [_normalise_code(error_code)] if error_code else _codes_in(text or "")
    family = _family(brand)
    for code in codes:
        if family:
            hit = ERROR_CODES.get(family, {}).get(code)
            if hit:
                return Diagnosis(hit[0], (hit[1],), 0.9, f"error {code} on {brand}")
            continue
        # No brand: only a code that means the same component for every brand that uses it.
        hits = {table[code] for table in ERROR_CODES.values() if code in table}
        if len(hits) == 1:
            component, kind = hits.pop()
            if category in (None, kind):
                return Diagnosis(component, (kind,), 0.8, f"error {code}")
    for pattern, component, kinds in SYMPTOMS:
        if text and pattern.search(text) and (category is None or category in kinds):
            return Diagnosis(component, kinds if category is None else (category,), 0.75, "symptom described")
    return None


def _same(a: Any, b: Any) -> bool:
    return re.sub(r"[\s-]", "", str(a or "")).lower() == re.sub(r"[\s-]", "", str(b or "")).lower()


def _catalogue_parts(component: str) -> list[dict[str, Any]]:
    """Parts in stock whose title names the component."""
    result = firebase_store.search_marketplace(
        query=component, item_type="part", category=None, brand=None,
        min_price=None, max_price=None, cursor=None, limit=50,
    )
    return [
        item for item in result.get("items", [])
        if item.get("type") == "part" and str(item.get("title") or "").lower().startswith(component.lower())
    ]


def best_part(diagnosis: Diagnosis, parts: Iterable[dict[str, Any]], brand: str | None,
              model: str | None) -> dict[str, Any] | None:
    """The part for the diagnosis: same model, else same brand, else the same appliance type."""
    candidates = [
        part for part in parts
        if str(part.get("category") or "").lower() in diagnosis.categories
    ]
    if not candidates:
        return None

    def rank(part: dict[str, Any]) -> tuple[int, int, float]:
        return (
            0 if model and _same(part.get("model"), model) else 1,
            0 if brand and _same(part.get("brand"), brand) else 1,
            float(part.get("price") or 0),
        )

    part = min(candidates, key=rank)
    fits = bool(model) and _same(part.get("model"), model)
    compatible: bool | None = True if fits else (False if model else None)
    confidence = diagnosis.confidence + (0.05 if fits else 0) - (0.1 if compatible is False else 0)
    return {
        "id": part["id"],
        "type": "part",
        "name": part.get("title"),
        "brand": part.get("brand"),
        "model": part.get("model"),
        "category": part.get("category"),
        "price_cents": round(float(part.get("price") or 0) * 100),
        "image_url": (part.get("image_urls") or [None])[0] or part.get("image_url"),
        "compatible": compatible,
        "confidence": round(min(confidence, 1.0), 2),
        "reason": diagnosis.reason,
    }


def suggest(text: str = "", brand: str | None = None, model: str | None = None,
            category: str | None = None, error_code: str | None = None,
            parts_for: Callable[[str], list[dict[str, Any]]] | None = None) -> dict[str, Any] | None:
    """The spare part to offer, or None when nothing points to one with enough confidence."""
    category = category.strip().lower() if category else category_of(text)
    diagnosis = diagnose(text, brand=brand, error_code=error_code, category=category)
    if diagnosis is None:
        return None
    parts = (parts_for or _catalogue_parts)(diagnosis.component)
    part = best_part(diagnosis, parts, brand, model)
    if part is None or part["confidence"] < MIN_CONFIDENCE:
        return None
    return part


def attach_to_response(response: dict[str, Any], message: str) -> dict[str, Any]:
    """Adds ``suggested_part`` to a manual answer of the chat; the rest of the JSON is unchanged.

    The user's message (with its error code or symptom), the answer and the
    appliance found by the agent are read. Any failure leaves the answer as it is.
    """
    if not response.get("is_manual"):
        return response
    try:
        text = "\n".join([message or "", str(response.get("issue_title") or "")])
        part = suggest(
            text=text,
            brand=response.get("brand"),
            model=response.get("model"),
            category=category_of(message, str(response.get("appliance_name") or ""), str(response.get("reply") or "")),
        )
    except Exception as exc:  # noqa: BLE001 - the answer must never fail because of the catalogue
        logger.warning("No spare part suggestion: %s", exc)
        part = None
    return {**response, "suggested_part": part}
