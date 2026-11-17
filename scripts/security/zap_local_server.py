"""Run the backend on this computer with an in-memory Firestore, for the OWASP ZAP scan (#159).

    python scripts/security/zap_local_server.py            # http://127.0.0.1:8080

Nothing reaches Firebase, Stripe (simulated checkout), OpenRouter or Kie: the
real endpoints, validation, auth and rate limits run on fake data, so ZAP can
attack them without touching production. The seed catalogue is loaded and two
users get a listing, a cart line and a paid order, so the IDOR checks have
something to steal.

The JWTs of the two test users (HS256, local secret) are written to
``scripts/security/out/tokens.json``; ZAP sends user A's as the bearer token.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "scripts" / "security" / "out"
SECRET = "local-zap-secret-not-used-anywhere-else-0123456789"

# Settings before the app is imported: local JWT secret, no third-party keys.
os.environ.update({
    "JWT_SECRET": SECRET,
    "ALLOWED_ORIGINS": "http://localhost:3000",
    "LISTING_IMAGES_BACKEND": "firestore",
    # A local webhook secret, so a webhook without a valid Stripe signature is checked (and refused).
    "STRIPE_WEBHOOK_SECRET": "whsec_local_zap_only",
})
for name in ("JWT_JWKS_URL", "STRIPE_SECRET_KEY", "OPENROUTER_API_KEY", "KIE_API_KEY",
             "FIREBASE_SERVICE_ACCOUNT_JSON"):
    os.environ.pop(name, None)
sys.path[:0] = [str(ROOT / "api"), str(ROOT / "tests")]

import catalogue_import
import firebase_store
import jwt
import uvicorn
from firebase_admin import firestore
from test_forum_store import Batch, Collection, Db, Inc

firestore.Increment = Inc  # the fake applies Inc counters


_where = Collection.where


def _where_with_filter(self, field=None, op=None, value=None, *, filter=None):
    """The fake also takes ``where(filter=FieldFilter(...))``, as firebase_store writes it."""
    if filter is not None:
        field, op, value = filter.field_path, filter.op_string, filter.value
    return _where(self, field, op, value)


Collection.where = _where_with_filter


class _Batch(Batch):
    def set(self, ref, data, merge=False):  # catalogue import writes with merge=True
        super().set(ref, data)


class LocalDb(Db):
    def batch(self):
        return _Batch(self)


DB = LocalDb()
firebase_store.get_firestore_client = lambda: DB


def token(uid: str, name: str) -> str:
    return jwt.encode({"sub": uid, "name": name, "exp": int(time.time()) + 7 * 24 * 3600}, SECRET, algorithm="HS256")


def seed() -> dict[str, str]:
    catalogue_import.import_seed(catalogue_import.FirestoreCatalogue(DB), ROOT / "scripts" / "seed", dry_run=False)
    now = int(time.time() * 1000)
    for uid, listing in (("user-a", "listing-a"), ("user-b", "listing-b")):
        DB.docs[f"listings/{listing}"] = {
            "title": f"Fridge of {uid}", "description": "Works well, collected from home.", "price": 120.0,
            "category": "refrigerator", "owner_uid": uid, "seller_name": uid, "status": "active", "quantity": 1,
            "condition": "used-good", "listing_kind": "appliance", "image_urls": [], "created_at": now,
            "handover_methods": ["pickup"], "pickup_zip": "28001", "type": "used",
        }
        DB.docs[f"carts/cart-{uid}"] = {
            "uid": uid, "product_type": "part", "product_id": "HDW-P-001", "quantity": 1,
            "added_price_cents": 2490, "added_at": now,
        }
        DB.docs[f"orders/order-{uid}"] = {
            "uid": uid, "checkout_id": f"co-{uid}", "status": "paid", "created_at": now, "paid_at": now,
            "seller_id": "hodowo", "seller_name": "HoDoWo", "currency": "eur", "subtotal_cents": 2490,
            "shipping_cents": 0, "total_cents": 2490, "shipping_method": "shipping", "delivery": None,
            "items": [{"product_type": "part", "product_id": "HDW-P-001", "title": "Drain pump", "quantity": 1,
                       "unit_price_cents": 2490, "price_cents": 2490, "offer_id": None}],
        }
    tokens = {uid: token(uid, f"Zap Tester {uid[-1].upper()}") for uid in ("user-a", "user-b", "user-c")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tokens.json").write_text(json.dumps(tokens, indent=2))
    return tokens


if __name__ == "__main__":
    seed()
    from index import app

    port = int(os.getenv("PORT", "8080"))
    print(f"HoDoWo backend on http://127.0.0.1:{port} (in-memory Firestore), tokens in {OUT / 'tokens.json'}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
