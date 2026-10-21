"""Time left for the current chat request.

Vercel stops a function after 300 seconds. A photo question runs several slow
steps (agent, manual retrieval, Kie image overlay), so each request gets a
budget and the slow steps check how much of it is left: when time is short
the image overlay is skipped and the guide is answered without it, instead of
the whole request being killed by Vercel.

The deadline is kept in a context variable, so concurrent requests on the same
instance do not share it (LangChain copies the context into tool threads).
"""

from __future__ import annotations

import contextvars
import math
import os
import time

# Vercel's limit is 300 s: keep a margin to format, save and send the answer.
DEFAULT_REQUEST_BUDGET_SECONDS = 240

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("request_deadline", default=None)


def start(seconds: float | None = None) -> None:
    """Starts the budget of the current request."""
    if seconds is None:
        seconds = float(os.getenv("REQUEST_BUDGET_SECONDS", DEFAULT_REQUEST_BUDGET_SECONDS))
    _deadline.set(time.monotonic() + seconds)


def remaining() -> float:
    """Seconds left for the current request (infinite outside a request)."""
    deadline = _deadline.get()
    return math.inf if deadline is None else deadline - time.monotonic()
