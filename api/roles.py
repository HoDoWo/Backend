"""Roles carried by the Firebase ID token as custom claims.

    admin     ``admin: true`` or ``role: "admin"``: moderates the forum and
              manages every partner's catalogue
    partner   ``role: "partner"`` and ``partner_id: "<partners doc id>"``:
              manages that partner's products only

Custom claims are set only with the Admin SDK (``scripts/set_user_role.py``),
so a client cannot give itself a role. Everyone else is a regular user.
"""

from __future__ import annotations

from typing import Any


def is_admin(user: dict[str, Any]) -> bool:
    return user.get("admin") is True or user.get("role") == "admin"


def partner_id_of(user: dict[str, Any]) -> str | None:
    """The partner a partner account acts for; None for everyone else."""
    if user.get("role") != "partner":
        return None
    partner_id = str(user.get("partner_id") or "").strip()
    return partner_id or None
