"""Give or take away the forum admin role (#44) through a Firebase custom claim.

Run it from the repository root, with the Firebase variables set in .env:

    python scripts/set_forum_admin.py someone@example.com           # make admin
    python scripts/set_forum_admin.py someone@example.com --remove  # back to a normal user

An admin can hide or restore any forum thread or reply
(POST /api/forum/threads/{id}/moderation). The claim reaches the app with the
next ID token: the user must sign out and in again, or wait up to one hour.
Other custom claims of the user are kept.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import firebase_store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("email", help="email the user signs in with")
    parser.add_argument("--remove", action="store_true", help="remove the admin role")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    from firebase_admin import auth

    firebase_store.get_firestore_client()  # initialises the Firebase app from .env
    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        print(f"No Firebase user with email {args.email}", file=sys.stderr)
        return 1

    claims = dict(user.custom_claims or {})
    if args.remove:
        claims.pop("admin", None)
    else:
        claims["admin"] = True
    auth.set_custom_user_claims(user.uid, claims or None)
    print(f"{args.email}: forum admin {'removed' if args.remove else 'granted'}. Sign in again to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
