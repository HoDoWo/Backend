"""In-memory fakes shared by the marketplace tests (#42, #43)."""

from __future__ import annotations

import copy

import forum_store
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



class FakeForumStore:
    """In-memory stand-in for forum_store, with the same ordering and paging rules."""

    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.replies: dict[str, dict[str, dict]] = {}
        self.votes: dict[str, dict] = {}
        self.reports: dict[str, dict] = {}
        self._next = 0

    def _id(self, prefix):
        self._next += 1
        return f"{prefix}{self._next:03d}"

    def _post(self, thread_id, reply_id):
        return self.replies[thread_id][reply_id] if reply_id else self.threads[thread_id]

    @staticmethod
    def _page(rows, key, limit, cursor):
        ids = [row[key] for row in rows]
        if cursor:
            if cursor not in ids:
                raise forum_store.InvalidCursor(cursor)
            rows = rows[ids.index(cursor) + 1:]
        return copy.deepcopy(rows[:limit]), len(rows) > limit

    def list_category_threads(self, category, order, limit, cursor):
        rows = [{**t, "thread_id": tid} for tid, t in self.threads.items()
                if t["category"] == category and t["status"] == "visible"]
        if order == "popular":
            rows.sort(key=lambda t: (-t["upvotes"], -t["last_activity_at"]))
        else:
            rows.sort(key=lambda t: -t["last_activity_at"])
        return self._page(rows, "thread_id", limit, cursor)

    def list_product_threads(self, product_key):
        return [{**copy.deepcopy(t), "thread_id": tid} for tid, t in self.threads.items()
                if t.get("product_key") == product_key]

    def get_thread(self, thread_id):
        thread = self.threads.get(thread_id)
        return {**copy.deepcopy(thread), "thread_id": thread_id} if thread else None

    def create_thread(self, data):
        thread_id = self._id("t")
        self.threads[thread_id] = copy.deepcopy(data)
        self.replies[thread_id] = {}
        return thread_id

    def list_replies(self, thread_id, limit, cursor):
        rows = [{**r, "reply_id": rid} for rid, r in self.replies.get(thread_id, {}).items()
                if r["status"] == "visible"]
        rows.sort(key=lambda r: r["created_at"])
        return self._page(rows, "reply_id", limit, cursor)

    def all_replies(self, thread_id):
        return [{**copy.deepcopy(r), "reply_id": rid} for rid, r in self.replies.get(thread_id, {}).items()]

    def get_reply(self, thread_id, reply_id):
        reply = self.replies.get(thread_id, {}).get(reply_id)
        return {**copy.deepcopy(reply), "reply_id": reply_id} if reply else None

    def create_reply(self, thread_id, data):
        reply_id = self._id("r")
        self.replies[thread_id][reply_id] = copy.deepcopy(data)
        self.threads[thread_id]["reply_count"] += 1
        self.threads[thread_id]["last_activity_at"] = data["created_at"]
        return reply_id

    def update_post(self, thread_id, reply_id, fields):
        self._post(thread_id, reply_id).update(fields)

    def set_status(self, thread_id, reply_id, status, reply_count_change=0):
        self._post(thread_id, reply_id)["status"] = status
        if reply_id:
            self.threads[thread_id]["reply_count"] += reply_count_change

    def voted(self, vote_ids):
        return {vote_id for vote_id in vote_ids if vote_id in self.votes}

    def add_vote(self, vote_id, data, thread_id, reply_id):
        if vote_id in self.votes:
            return False
        self.votes[vote_id] = dict(data)
        self._post(thread_id, reply_id)["upvotes"] += 1
        return True

    def remove_vote(self, vote_id, thread_id, reply_id):
        if self.votes.pop(vote_id, None) is None:
            return False
        self._post(thread_id, reply_id)["upvotes"] -= 1
        return True

    def add_report(self, report_id, data, thread_id, reply_id):
        if report_id in self.reports:
            return False
        self.reports[report_id] = dict(data)
        self._post(thread_id, reply_id)["report_count"] += 1
        return True


class FakePartnerStore:
    """In-memory stand-in for partner_store."""

    def __init__(self, partners=None, products=None):
        self.partners: dict[str, dict] = copy.deepcopy(partners or {})
        self.products: dict[str, dict] = copy.deepcopy(products or {})

    def list_products(self):
        return [{**copy.deepcopy(p), "id": pid} for pid, p in self.products.items()]

    def get_product(self, product_id):
        product = self.products.get(product_id)
        return {**copy.deepcopy(product), "id": product_id} if product else None

    def create_product(self, product_id, data):
        if product_id in self.products:
            return False
        self.products[product_id] = copy.deepcopy(data)
        return True

    def update_product(self, product_id, fields):
        self.products[product_id].update(copy.deepcopy(fields))

    def list_partners(self):
        return copy.deepcopy(self.partners)

    def get_partner(self, partner_id):
        partner = self.partners.get(partner_id)
        return copy.deepcopy(partner) if partner is not None else None
