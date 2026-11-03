"""In-memory fakes shared by the marketplace tests (#42, #43)."""

from __future__ import annotations

import copy

import marketplace_store


class FakeStore:
    """In-memory stand-in for marketplace_store."""

    def __init__(self):
        self.cart_items: dict[str, dict] = {}
        self.items: dict[tuple[str, str], dict] = {}
        self.orders: dict[str, dict] = {}
        self.events: set[str] = set()

    # catalogue and cart
    def get_cart_items(self, uid):
        return [
            {**copy.deepcopy(line), "cart_item_id": line_id}
            for line_id, line in self.cart_items.items()
            if line["uid"] == uid
        ]

    def get_cart_item(self, cart_item_id):
        line = self.cart_items.get(cart_item_id)
        return {**copy.deepcopy(line), "cart_item_id": cart_item_id} if line else None

    def save_cart_item(self, cart_item_id, data):
        self.cart_items[cart_item_id] = copy.deepcopy(data)

    def update_cart_item(self, cart_item_id, fields):
        self.cart_items[cart_item_id].update(fields)

    def delete_cart_item(self, cart_item_id):
        del self.cart_items[cart_item_id]

    # test helpers
    def set_cart(self, uid, lines):
        self.cart_items = {k: v for k, v in self.cart_items.items() if v["uid"] != uid}
        for line in lines:
            line = dict(line)
            self.cart_items[line.pop("cart_item_id")] = {"uid": uid, **line}

    def cart(self, uid):
        return self.get_cart_items(uid)

    def get_item(self, product_type, product_id):
        item = self.items.get((product_type, product_id))
        if item is None:
            return None
        _, seller_field = marketplace_store.PRODUCT_SOURCES[product_type]
        return {**item, "seller_id": item.get(seller_field) or marketplace_store.PLATFORM_SELLER_ID}

    # orders
    def save_order(self, order_id, order):
        self.orders[order_id] = copy.deepcopy(order)

    def update_order(self, order_id, fields):
        self.orders[order_id].update(fields)

    def get_order(self, order_id):
        order = self.orders.get(order_id)
        return {**copy.deepcopy(order), "order_id": order_id} if order else None

    def get_orders_by_checkout(self, checkout_id):
        return [copy.deepcopy(o) for o in self.orders.values() if o["checkout_id"] == checkout_id]

    def list_user_orders(self, uid, limit):
        orders = [copy.deepcopy(o) for o in self.orders.values() if o["uid"] == uid]
        return sorted(orders, key=lambda o: o["created_at"], reverse=True)[:limit]

    # webhook idempotency
    def claim_event(self, event_id, event_type, created_at):
        if event_id in self.events:
            return False
        self.events.add(event_id)
        return True

    def release_event(self, event_id):
        self.events.discard(event_id)

    def fulfil_order(self, order, paid_at):
        self.orders[order["order_id"]].update(status="paid", paid_at=paid_at)
        for item in order["items"]:
            stored = self.items[(item["product_type"], item["product_id"])]
            if item["product_type"] == "used":
                offered = 1 if stored.get("quantity") is None else stored["quantity"]
                stored["quantity"] = max(offered - item["quantity"], 0)
                if stored["quantity"] == 0:
                    stored["status"] = "sold"
            else:
                stored["stock"] -= item["quantity"]
        for cart_item_id in order["cart_item_ids"]:
            self.cart_items.pop(cart_item_id, None)
