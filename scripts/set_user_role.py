"""Give a user the partner or admin role through Firebase custom claims.

Run it from the repository root, with the Firebase variables set in .env:

    python scripts/set_user_role.py someone@example.com --partner voltacasa  # manages VoltaCasa's products
    python scripts/set_user_role.py someone@example.com --admin              # admin (all partners, forum)
    python scripts/set_user_role.py someone@example.com --remove             # back to a regular user

The partner must exist in the ``partners`` collection. The role reaches the
app with the next ID token: the user must sign out and in again, or wait up
to one hour. Other custom claims of the user are kept.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import firebase_store  # noqa: E402
import partner_store  # noqa: E402

ROLE_CLAIMS = ("role", "partner_id", "admin")


def new_claims(current: dict | None, partner: str | None, admin: bool) -> dict:
    """Claims after the change: the role keys are replaced, the others kept."""
    claims = {key: value for key, value in (current or {}).items() if key not in ROLE_CLAIMS}
    if partner:
        claims.update(role="partner", partner_id=partner)
    elif admin:
        claims.update(role="admin", admin=True)
    return claims


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("email", help="email the user signs in with")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--partner", metavar="PARTNER_ID", help="id of the partner in the partners collection")
    group.add_argument("--admin", action="store_true", help="make the user an admin")
    group.add_argument("--remove", action="store_true", help="remove the partner or admin role")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from firebase_admin import auth

    firebase_store.get_firestore_client()  # initialises the Firebase app from .env
    if args.partner and partner_store.get_partner(args.partner) is None:
        print(f"No partner with id {args.partner!r} in the partners collection", file=sys.stderr)
        return 1
    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        print(f"No Firebase user with email {args.email}", file=sys.stderr)
        return 1

    claims = new_claims(user.custom_claims, args.partner, args.admin)
    auth.set_custom_user_claims(user.uid, claims or None)
    role = f"partner of {args.partner}" if args.partner else "admin" if args.admin else "regular user"
    print(f"{args.email} is now a {role}. Sign in again to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
