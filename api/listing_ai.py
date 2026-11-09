"""AI helpers for the "Sell an appliance or part" form.

Three small jobs share the chat model configured in :mod:`config`:

* read the model/part label in a photo (the scan button),
* tidy the seller's narrative (AI Polish),
* estimate a fair second-hand price from the form (valuation card).

The helpers return plain Python data; ``endpoints/listing_ai.py`` adds HTTP,
authentication and error mapping.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

import config
from logging_config import get_logger


logger = get_logger(__name__)

MAX_PART_TEXT = 80
MAX_DESCRIPTION = 800
MAX_RATIONALE = 300

SCAN_SYSTEM_PROMPT = """You are a computer-vision tool that reads model and part labels on home \
appliances and their components. Look at the photo and extract the manufacturer brand, the model \
code and the part number when they are printed on a sticker, badge or engraved plate.
Respond ONLY with a valid JSON object, no prose and no markdown, using exactly this schema:
{"brand": <string or null>, "model": <string or null>, "part_number": <string or null>, \
"confidence": <number from 0 to 1>, "visible_evidence": <short string>}
Never invent a code: use null when the value is not clearly visible. Keep the exact characters \
shown (letters, digits, dots and dashes), without spaces around them."""

POLISH_SYSTEM_PROMPT = """You are an expert marketplace copywriter for second-hand appliances and \
spare parts. Rewrite the seller's description so it reads clearly and professionally.
Rules: keep every fact the seller wrote, never invent details, keep it concise, use plain text \
(no markdown, no lists), capitalise sentences and end with a full stop. Reply with the rewritten \
description only, between 30 and 800 characters."""

ESTIMATE_SYSTEM_PROMPT = """You are a pricing assistant for a second-hand appliance and spare-part \
marketplace. Estimate a realistic selling range in US dollars (whole dollars, not cents) from the \
listing details the seller entered. Consider the item kind, category, brand, model, condition and \
what buyers usually pay in the community market.
Respond ONLY with a valid JSON object, no prose and no markdown, using exactly this schema:
{"low": <number>, "high": <number>, "currency": "USD", "rationale": <one short sentence>}
Return positive prices with low <= high. Do not include dollar signs in the numbers."""


def _image_data_url(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Listing image does not exist: {image_path}")

    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _text_of(content: Any) -> str:
    """LangChain may answer with a string or a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    raise RuntimeError("The AI returned a response without text")


def _json_object(content: Any) -> dict[str, Any]:
    text = _text_of(content).strip()
    if "```" in text:
        for part in text.split("```"):
            cleaned = part.removeprefix("json").strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                text = cleaned
                break
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Listing AI returned invalid JSON: %s", text[:200])
        raise RuntimeError("The AI returned an invalid response") from exc
    if not isinstance(data, dict):
        raise RuntimeError("The AI returned an invalid response")
    return data


def _optional_text(value: Any, limit: int = MAX_PART_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.replace("$", "").replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def scan_model_label(image_path: str, user_context: str = "") -> dict[str, Any]:
    """Read brand, model and part number from a label photo."""
    image_url = _image_data_url(image_path)
    response = config.get_openrouter_llm().invoke(
        [
            ("system", SCAN_SYSTEM_PROMPT),
            (
                "human",
                [
                    {
                        "type": "text",
                        "text": user_context or "Read the model and part number in this photo.",
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            ),
        ]
    )
    data = _json_object(response.content)

    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.0
    confidence = min(max(float(confidence), 0.0), 1.0)

    result = {
        "brand": _optional_text(data.get("brand")),
        "model": _optional_text(data.get("model")),
        "part_number": _optional_text(data.get("part_number")),
        "confidence": confidence,
        "visible_evidence": _optional_text(data.get("visible_evidence"), 300) or "",
    }
    logger.info(
        "Label scan finished (brand=%s, model=%s, part=%s, confidence=%.2f)",
        result["brand"], result["model"], result["part_number"], confidence,
    )
    return result


def polish_listing(description: str) -> str:
    """Return a tidied version of the seller's narrative."""
    cleaned = " ".join(str(description or "").split())
    if not cleaned:
        return ""
    if len(cleaned) < 10:
        return cleaned

    response = config.get_openrouter_llm().invoke(
        [
            ("system", POLISH_SYSTEM_PROMPT),
            ("human", cleaned[:MAX_DESCRIPTION]),
        ]
    )
    polished = _text_of(response.content).strip().strip('"').strip()
    if "```" in polished:
        polished = polished.replace("```", "").strip()
    if not polished:
        raise RuntimeError("The AI returned an empty description")
    return polished[:MAX_DESCRIPTION]


def estimate_listing(details: dict[str, Any]) -> dict[str, Any]:
    """Estimate a fair USD price range from the form the seller filled in."""
    def line(label: str, value: Any) -> str:
        text = " ".join(str(value or "").split())[:MAX_DESCRIPTION]
        return f"{label}: {text or '-'}"

    context = "\n".join(
        [
            line("Listing title", details.get("title")),
            line("Description", details.get("description")),
            line("Item kind", details.get("listing_kind")),
            line("Category", details.get("category")),
            line("Brand", details.get("brand")),
            line("Model / part number", details.get("model")),
            line("Condition", details.get("condition")),
            line("Number of photos", details.get("photo_count", 0)),
            line("Pickup available", "yes" if details.get("pickup") else "no"),
            line("Shipping available", "yes" if details.get("shipping") else "no"),
            line("Pickup location", details.get("location")),
        ]
    )

    response = config.get_openrouter_llm().invoke(
        [
            ("system", ESTIMATE_SYSTEM_PROMPT),
            ("human", context),
        ]
    )
    data = _json_object(response.content)

    low = _positive_number(data.get("low"))
    high = _positive_number(data.get("high"))
    if low is None or high is None:
        raise RuntimeError("The AI estimate did not contain valid prices")
    if high < low:
        low, high = high, low

    result = {
        "low": round(low, 2),
        "high": round(high, 2),
        "currency": _optional_text(data.get("currency"), 8) or "USD",
        "rationale": _optional_text(data.get("rationale"), MAX_RATIONALE) or "",
    }
    logger.info("Price estimate finished: %s-%s %s", result["low"], result["high"], result["currency"])
    return result
