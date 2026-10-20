"""Daily energy-saving tip shown on the Home screen ("Helpful Tip")."""

from __future__ import annotations

import datetime

import auth
import config
import fastapi
import logging_config
from fastapi import HTTPException, params

logger = logging_config.get_logger(__name__)

environmental_tip_router = fastapi.APIRouter()

MAX_TIP_LENGTH = 240

SYSTEM_PROMPT = (
    "You are an expert in home appliance energy efficiency and environmental "
    "sustainability. Give ONE practical electricity-saving or environmental tip "
    "about household appliances, in one short sentence (max 25 words). "
    "Answer with the tip only: no title, no quotes, no markdown."
)

# One tip per day is enough: it is cached per (process, date) so the Home
# screen does not call the LLM every time it is opened.
_tip_cache: dict[str, str] = {}


def clean_tip(raw: object) -> str:
    """Turn the model answer into a single short line of plain text."""
    text = " ".join(str(raw or "").split())
    text = text.strip().strip('"').strip("*").strip()
    if len(text) > MAX_TIP_LENGTH:
        text = text[: MAX_TIP_LENGTH - 3].rstrip() + "..."
    return text


def _generate_tip() -> str:
    answer = config.get_openrouter_llm().invoke(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Give me today's appliance energy-saving tip."},
        ]
    )
    return clean_tip(getattr(answer, "content", answer))


@environmental_tip_router.get("/api/appliances/environmental-tip")
@config.ai_rate_limit
async def get_environmental_tip(
    request: fastapi.Request,
    user: dict = params.Depends(auth.get_current_user),
) -> dict[str, str]:
    """Return today's tip as {"tip": "..."}; 502 when the model fails."""
    today = datetime.date.today().isoformat()
    cached = _tip_cache.get(today)
    if cached:
        return {"tip": cached}

    try:
        tip = _generate_tip()
    except Exception as exc:  # noqa: BLE001 - any model/network failure
        logger.error("Environmental tip generation failed: %s", exc)
        raise HTTPException(status_code=502, detail="tip generation failed") from exc

    if not tip:
        raise HTTPException(status_code=502, detail="empty tip")

    _tip_cache.clear()
    _tip_cache[today] = tip
    return {"tip": tip}
