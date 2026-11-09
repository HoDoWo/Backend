"""Firestore access for the official partner catalogue (#39).

    partners/{partnerId}                      the official vendors
    partner_products/{partnerId}_{sku}        their new products

The ids are the same ones the catalogue import (#24, #33) uses, so a product
written through the API and the same SKU in a partner feed are one document.
Only this backend writes these collections (``firestore.rules``).
"""

from __future__ import annotations

from typing import Any

import firebase_store
from google.api_core import exceptions as google_exceptions

PRODUCTS = "partner_products"
PARTNERS = "partners"


def _db():
    return firebase_store.get_firestore_client()


def list_products() -> list[dict[str, Any]]:
    return [{**(doc.to_dict() or {}), "id": doc.id} for doc in _db().collection(PRODUCTS).stream()]


def get_product(product_id: str) -> dict[str, Any] | None:
    snapshot = _db().collection(PRODUCTS).document(product_id).get()
    return {**(snapshot.to_dict() or {}), "id": product_id} if snapshot.exists else None


def create_product(product_id: str, data: dict[str, Any]) -> bool:
    """Write a new product; False when the id (partner + SKU) already exists."""
    try:
        _db().collection(PRODUCTS).document(product_id).create(data)
    except google_exceptions.Conflict:
        return False
    return True


def update_product(product_id: str, fields: dict[str, Any]) -> None:
    _db().collection(PRODUCTS).document(product_id).update(fields)


def list_partners() -> dict[str, dict[str, Any]]:
    return {doc.id: doc.to_dict() or {} for doc in _db().collection(PARTNERS).stream()}


def get_partner(partner_id: str) -> dict[str, Any] | None:
    snapshot = _db().collection(PARTNERS).document(partner_id).get()
    return snapshot.to_dict() or {} if snapshot.exists else None
