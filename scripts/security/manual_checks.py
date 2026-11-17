"""Manual security checks of #159 that a scanner cannot judge: IDOR, webhook signature, limits.

    python scripts/security/manual_checks.py http://127.0.0.1:8080

The tokens are read from ``scripts/security/out/tokens.json`` (git-ignored); it has the JWTs of three test users, ``user-a``, ``user-b`` and ``user-c``
(written by zap_local_server.py; for a preview deployment, two Firebase test
users). User A owns ``listing-a``, ``cart-user-a`` and ``order-user-a``; user B
tries to read and change them. Prints a Markdown table and exits with 1 when a
check fails. Run it only against the local server or a preview deployment.
"""

from __future__ import annotations

import io
import json
import pathlib
import sys

import requests
from PIL import Image

# Always this file, never a path given on the command line.
TOKENS = pathlib.Path(__file__).resolve().parent / "out" / "tokens.json"


def main(base: str) -> int:
    tokens = json.loads(TOKENS.read_text())
    a = {"Authorization": f"Bearer {tokens['user-a']}"}
    b = {"Authorization": f"Bearer {tokens['user-b']}"}
    rows: list[tuple[str, str, bool]] = []

    def check(name: str, response: requests.Response, ok: set[int], extra: bool = True) -> None:
        rows.append((name, f"{response.status_code}", response.status_code in ok and extra))

    def url(path: str) -> str:
        return base.rstrip("/") + path

    # IDOR: user B on user A's data.
    check("B reads A's order", requests.get(url("/api/marketplace/orders/order-user-a"), headers=b), {403, 404})
    check("B edits A's listing", requests.patch(url("/api/marketplace/listings/listing-a"), headers=b,
                                                json={"title": "Stolen fridge"}), {403, 404})
    check("B marks A's listing sold", requests.post(url("/api/marketplace/listings/listing-a/sold"), headers=b),
          {403, 404})
    check("B deletes A's listing", requests.delete(url("/api/marketplace/listings/listing-a"), headers=b), {403, 404})
    check("B changes A's cart line", requests.patch(url("/api/marketplace/cart/items/cart-user-a"), headers=b,
                                                    json={"quantity": 3}), {403, 404})
    check("B removes A's cart line", requests.delete(url("/api/marketplace/cart/items/cart-user-a"), headers=b),
          {403, 404})
    cart_b = requests.get(url("/api/marketplace/cart"), headers=b)
    check("B's cart has no line of A", cart_b, {200},
          all(line.get("cart_item_id") != "cart-user-a" for line in cart_b.json().get("items", [])))
    orders_b = requests.get(url("/api/marketplace/orders"), headers=b)
    check("B's orders have no order of A", orders_b, {200}, "order-user-a" not in orders_b.text)
    listing_a = requests.get(url("/api/marketplace/listings/listing-a"), headers=a)
    check("A's listing still A's after the attempts", listing_a, {200},
          listing_a.json().get("title") == "Fridge of user-a")

    # Conversations: a stranger cannot read a conversation of two other users.
    started = requests.post(url("/api/marketplace/conversations"), headers=a,
                            json={"listing_id": "listing-b", "text": "Hi, is it available?"})
    conversation = started.json().get("id") or started.json().get("conversation", {}).get("id")
    stranger = {"Authorization": f"Bearer {tokens.get('user-c', tokens['user-b'])}"}
    if "user-c" in tokens and conversation:
        check("C reads A-B conversation", requests.get(url(f"/api/marketplace/conversations/{conversation}"),
                                                       headers=stranger), {403, 404})
    check("Seller cannot accept an offer that does not exist",
          requests.post(url(f"/api/marketplace/conversations/{conversation}/offer/accept"), headers=a), {404, 409, 422})

    # No token, wrong token.
    check("Cart without a token", requests.get(url("/api/marketplace/cart")), {401})
    check("Orders with a forged token", requests.get(url("/api/marketplace/orders"),
                                                     headers={"Authorization": "Bearer eyJhbGciOiJub25lIn0.e30."}),
          {401})

    # Stripe webhook without (or with a wrong) signature.
    event = json.dumps({"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {}}})
    check("Webhook without signature", requests.post(url("/api/marketplace/stripe/webhook"), data=event), {400})
    check("Webhook with a forged signature", requests.post(url("/api/marketplace/stripe/webhook"), data=event,
                                                           headers={"Stripe-Signature": "t=1,v1=deadbeef"}),
          {400})

    # Image upload limits: 5 MB per photo.
    big = io.BytesIO()
    Image.effect_noise((2600, 2600), 100).convert("RGB").save(big, "PNG")
    check(f"Upload of {big.tell() // 1_000_000} MB photo", requests.post(
        url("/api/marketplace/listings/images"), headers=a,
        files={"image": ("big.png", big.getvalue(), "image/png")}), {413})
    check("Upload of a non-image", requests.post(url("/api/marketplace/listings/images"), headers=a,
                                                 files={"image": ("x.png", b"<?php echo 1; ?>", "image/png")}),
          {415, 422})
    seven = {"title": "Seven photos", "description": "A listing with too many photos attached to it.",
             "price": 10, "category": "other", "condition": "used-good", "listing_kind": "part",
             "handover_methods": ["shipping"], "shipping_price_cents": 500,
             "image_urls": [f"{base}/api/marketplace/listings/images/{i}" for i in range(7)]}
    check("Listing with 7 photos", requests.post(url("/api/marketplace/listings"), headers=a, json=seven), {422})

    # Rate limit of forum posts (10 per minute per user).
    statuses = [
        requests.post(url("/api/forum/threads"), headers=b, json={"title": f"Question number {i} about pumps",
                                                                   "category": "washing-machine"}).status_code
        for i in range(12)
    ]
    rows.append(("12 forum posts in a minute", " ".join(map(str, statuses)), 429 in statuses))

    print("| Check | Status | Result |\n|---|---|---|")
    for name, status, passed in rows:
        print(f"| {name} | {status} | {'✅ pass' if passed else '❌ FAIL'} |")
    return 0 if all(passed for _, _, passed in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
