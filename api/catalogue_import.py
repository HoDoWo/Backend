"""Catalogue import: curated seed (#33) and partner feeds (#24).

Partners send their catalogue as a CSV or JSON file; we never scrape their
websites. The same code loads the curated seed in ``scripts/seed/``.

* Every document has a stable id, so running an import twice never creates
  duplicates: ``partners/{id}``, ``partner_products/{partner_id}_{sku}``,
  ``parts/{sku}`` and ``part_compatibility/{sku}_{brand}_{model}``.
* Invalid rows are skipped and reported with their line number and reason.
* With a full feed (``full=True``, always for the seed), a partner's products
  missing from it are set to ``stock: 0`` (out of stock), never deleted:
  orders may still point at them. A partial feed only adds and updates.
* ``dry_run`` computes the same report without writing anything.
* One bad feed never stops the others; the run just ends with an error.

Field names follow the Sprint 2 ER diagram; dates are epoch milliseconds.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import pathlib
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

import firebase_store

BATCH_LIMIT = 450  # Firestore allows 500 writes per batch
TRUE = {"true", "1", "yes", "y"}
FALSE = {"false", "0", "no", "n", ""}
# Fields the import compares; created_at / updated_at are managed here.
TIMESTAMP_FIELDS = ("created_at", "updated_at")

# Minimum content of the curated seed (#33 acceptance criteria).
SEED_MINIMUMS = {"partners": 4, "partner_tiers": 2, "partner_products": 60, "parts": 40, "compatible_models": 10}


class RowError(ValueError):
    """A row that cannot be imported; the message is shown in the report."""


class FeedError(RuntimeError):
    """A whole feed that cannot be read (missing file, bad JSON, unknown partner)."""


def now_ms() -> int:
    return int(time.time() * 1000)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


# --- value parsing ----------------------------------------------------------


def _text(row: dict, field: str, *, required: bool = True, max_len: int = 200) -> str | None:
    value = str(row.get(field) or "").strip()
    if not value:
        if required:
            raise RowError(f"{field} is required")
        return None
    if len(value) > max_len:
        raise RowError(f"{field} is longer than {max_len} characters")
    return value


def _id_part(row: dict, field: str) -> str:
    value = _text(row, field, max_len=80)
    if "/" in value:
        raise RowError(f"{field} cannot contain '/'")
    return value


def _cents(row: dict) -> int:
    """Price from price_cents (int) or price ("24.90" or "24,90")."""
    if str(row.get("price_cents") or "").strip():
        raw = str(row["price_cents"]).strip()
        if not raw.isdigit():
            raise RowError("price_cents must be a whole number")
        cents = int(raw)
    else:
        raw = str(row.get("price") or "").strip().replace(",", ".")
        if not raw:
            raise RowError("price is required")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            raise RowError(f"price {raw!r} is not a number") from None
        if amount != amount.quantize(Decimal("0.01")):
            raise RowError("price has more than 2 decimals")
        cents = int(amount * 100)
    if not 0 < cents <= 10_000_000:
        raise RowError("price must be between 0.01 and 100000")
    return cents


def _int(row: dict, field: str, *, default: int | None = None, low: int = 0, high: int = 100_000) -> int:
    raw = str(row.get(field) if row.get(field) is not None else "").strip()
    if not raw:
        if default is None:
            raise RowError(f"{field} is required")
        return default
    if not re.fullmatch(r"-?\d+", raw):
        raise RowError(f"{field} must be a whole number")
    value = int(raw)
    if not low <= value <= high:
        raise RowError(f"{field} must be between {low} and {high}")
    return value


def _bool(row: dict, field: str, default: bool = False) -> bool:
    raw = row.get(field)
    if isinstance(raw, bool):
        return raw
    raw = str(raw if raw is not None else "").strip().lower()
    if raw in TRUE:
        return True
    if raw in FALSE:
        return default if raw == "" else False
    raise RowError(f"{field} must be true or false")


def _image(row: dict) -> str | None:
    url = _text(row, "image_url", required=False, max_len=500)
    if url and not url.startswith("https://"):
        raise RowError("image_url must start with https://")
    return url


def _common(row: dict) -> dict[str, Any]:
    title = _text(row, "title", max_len=120)
    return {
        "title": title,
        "title_normalised": firebase_store._title_key(title),
        "brand": _text(row, "brand", max_len=80),
        "model": _text(row, "model", max_len=80),
        "category": _text(row, "category", max_len=80).lower(),
        "description": _text(row, "description", required=False, max_len=800) or "",
        "price_cents": _cents(row),
        "stock": _int(row, "stock"),
        "image_url": _image(row),
        "condition": "new",
    }


# --- row normalisers: (row, context) -> (doc_id, document) -------------------


def normalise_partner(row: dict, _ctx: dict) -> tuple[str, dict[str, Any]]:
    partner_id = slug(_id_part(row, "id"))
    commission = str(row.get("commission_pct") or "").strip().replace(",", ".")
    try:
        commission_pct = float(commission)
    except ValueError:
        raise RowError("commission_pct must be a number") from None
    if not 0 <= commission_pct <= 50:
        raise RowError("commission_pct must be between 0 and 50")
    return partner_id, {
        "name": _text(row, "name", max_len=120),
        "partner_tier": _int(row, "partner_tier", low=1, high=3),
        "commission_pct": commission_pct,
        "active": _bool(row, "active", default=True),
    }


def normalise_product(row: dict, ctx: dict) -> tuple[str, dict[str, Any]]:
    partner_id = ctx["partner_id"]
    row_partner = str(row.get("partner_id") or "").strip()
    if row_partner and slug(row_partner) != partner_id:
        raise RowError(f"partner_id {row_partner!r} does not match the feed partner {partner_id!r}")
    partner = ctx["partners"][partner_id]
    sku = _id_part(row, "sku")
    doc = _common(row)
    doc.update(
        partner_id=partner_id,
        partner_name=partner["name"],
        partner_tier=partner["partner_tier"],  # copy of PARTNER.partner_tier
        sku=sku,
        item_type="new",
        listing_kind="appliance",
        sponsored=_bool(row, "sponsored"),
    )
    return f"{partner_id}_{sku}", doc


def normalise_part(row: dict, ctx: dict) -> tuple[str, dict[str, Any]]:
    sku = _id_part(row, "sku")
    doc = _common(row)
    partner_id = slug(str(row.get("partner_id") or "").strip())
    if partner_id and partner_id not in ctx["partners"]:
        raise RowError(f"unknown partner_id {partner_id!r}")
    doc.update(
        sku=sku,
        part_number=_text(row, "part_number", required=False, max_len=80),
        partner_id=partner_id or None,  # None = sold by HoDoWo
        partner_name=ctx["partners"][partner_id]["name"] if partner_id else None,
        item_type="part",
        listing_kind="part",
    )
    return sku, doc


def normalise_compatibility(row: dict, ctx: dict) -> tuple[str, dict[str, Any]]:
    part_id = _id_part(row, "part_sku")
    if part_id not in ctx["part_ids"]:
        raise RowError(f"unknown part_sku {part_id!r}")
    brand = _text(row, "brand", max_len=80)
    model = _text(row, "model", max_len=80)
    return f"{part_id}_{slug(brand)}_{slug(model)}", {
        "part_id": part_id,
        "brand": brand,
        "model": model,
        "model_normalised": firebase_store._title_key(model).replace(" ", ""),
        "category": _text(row, "category", max_len=80).lower(),
    }


# --- reading files ----------------------------------------------------------


def read_rows(path: pathlib.Path) -> list[tuple[int, dict]]:
    """Return (line_number, row) pairs from a CSV or JSON file.

    JSON may be a list of objects or ``{"partner_id": ..., "products": [...]}``;
    its "line" is the position of the object in the list (1-based).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise FeedError(f"cannot read {path}: {exc}") from exc

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FeedError(f"{path.name} is not valid JSON: {exc}") from exc
        if isinstance(data, dict):
            data = data.get("products", data.get("items"))
        if not isinstance(data, list):
            raise FeedError(f"{path.name} must contain a list of products")
        return [(i, item if isinstance(item, dict) else {}) for i, item in enumerate(data, start=1)]

    if path.suffix.lower() != ".csv":
        raise FeedError(f"{path.name}: only .csv and .json feeds are supported")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise FeedError(f"{path.name} is empty")
    # Line 1 is the header, so the first data row is line 2.
    return [(reader.line_num, row) for row in reader]


# --- Firestore access (replaced by a fake in the tests) ---------------------


class FirestoreCatalogue:
    def __init__(self, client=None):
        self.client = client or firebase_store.get_firestore_client()

    def read(self, collection: str, field: str | None = None, value: Any = None) -> dict[str, dict]:
        ref = self.client.collection(collection)
        if field is not None:
            ref = ref.where(field, "==", value)
        return {snap.id: snap.to_dict() or {} for snap in ref.stream()}

    def write(self, collection: str, documents: dict[str, dict]) -> None:
        items = list(documents.items())
        for start in range(0, len(items), BATCH_LIMIT):
            batch = self.client.batch()
            for doc_id, data in items[start : start + BATCH_LIMIT]:
                batch.set(self.client.collection(collection).document(doc_id), data, merge=True)
            batch.commit()


# --- import -----------------------------------------------------------------


@dataclasses.dataclass
class Report:
    name: str
    new: list[str] = dataclasses.field(default_factory=list)
    updated: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    unchanged: int = 0
    delisted: list[str] = dataclasses.field(default_factory=list)
    rejected: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def summary(self) -> str:
        if self.error:
            return f"{self.name}: FAILED - {self.error}"
        parts = [f"{len(self.new)} new", f"{len(self.updated)} updated", f"{self.unchanged} unchanged",
                 f"{len(self.rejected)} rejected"]
        if self.delisted:
            parts.append(f"{len(self.delisted)} set out of stock")
        return f"{self.name}: " + ", ".join(parts)

    def details(self) -> list[str]:
        lines = [f"  + {doc_id}" for doc_id in self.new]
        lines += [f"  ~ {doc_id}: {', '.join(fields)}" for doc_id, fields in self.updated.items()]
        lines += [f"  0 {doc_id}: stock -> 0 (missing from the feed)" for doc_id in self.delisted]
        lines += [f"  ! {reason}" for reason in self.rejected]
        return lines


def _changed_fields(old: dict, new: dict) -> list[str]:
    return sorted(key for key, value in new.items() if key not in TIMESTAMP_FIELDS and old.get(key) != value)


def import_rows(
    store,
    report: Report,
    collection: str,
    rows: Iterable[tuple[int, dict]],
    normalise: Callable[[dict, dict], tuple[str, dict]],
    ctx: dict,
    existing: dict[str, dict],
    *,
    dry_run: bool,
    delist_missing: bool = False,
) -> dict[str, dict]:
    """Normalise rows, compare them with Firestore and write the changes."""
    stamp = now_ms()
    seen: dict[str, int] = {}
    writes: dict[str, dict] = {}
    accepted: dict[str, dict] = {}

    for line, row in rows:
        try:
            doc_id, doc = normalise(row, ctx)
        except RowError as exc:
            report.rejected.append(f"line {line}: {exc}")
            continue
        if doc_id in seen:
            report.rejected.append(f"line {line}: duplicate of line {seen[doc_id]} ({doc_id})")
            continue
        seen[doc_id] = line
        accepted[doc_id] = doc

        old = existing.get(doc_id)
        if old is None:
            report.new.append(doc_id)
            writes[doc_id] = {**doc, "created_at": stamp, "updated_at": stamp}
        elif changed := _changed_fields(old, doc):
            report.updated[doc_id] = changed
            writes[doc_id] = {**doc, "updated_at": stamp}  # created_at is kept
        else:
            report.unchanged += 1

    if delist_missing:
        for doc_id, old in existing.items():
            if doc_id not in seen and int(old.get("stock") or 0) > 0:
                report.delisted.append(doc_id)
                writes[doc_id] = {"stock": 0, "updated_at": stamp}

    if writes and not dry_run:
        store.write(collection, writes)
    return accepted


def import_partner_feed(store, partner_id: str, path: pathlib.Path, *, dry_run: bool, full: bool = False,
                        partners: dict[str, dict] | None = None) -> Report:
    """Import one partner's feed into partner_products (#24)."""
    partner_id = slug(partner_id)
    report = Report(f"partner_products ({partner_id}, {path.name})")
    try:
        partners = partners if partners is not None else store.read("partners")
        if partner_id not in partners:
            raise FeedError(f"unknown partner {partner_id!r}: add it to partners first")
        rows = read_rows(path)
        existing = store.read("partner_products", "partner_id", partner_id)
    except (FeedError, RuntimeError) as exc:
        report.error = str(exc)
        return report
    import_rows(store, report, "partner_products", rows, normalise_product,
                {"partner_id": partner_id, "partners": partners}, existing,
                dry_run=dry_run, delist_missing=full)
    return report


def import_seed(store, folder: pathlib.Path, *, dry_run: bool) -> list[Report]:
    """Load the curated seed (#33): partners, their products, parts, compatibility."""
    reports: list[Report] = []

    def run(name, file_name, collection, normalise, ctx, existing):
        report = Report(name)
        reports.append(report)
        try:
            rows = read_rows(folder / file_name)
        except FeedError as exc:
            report.error = str(exc)
            return {}
        return import_rows(store, report, collection, rows, normalise, ctx, existing, dry_run=dry_run)

    stored_partners = store.read("partners")
    partners = {**stored_partners, **run("partners", "partners.csv", "partners", normalise_partner, {}, stored_partners)}

    # Products are grouped by partner, so each partner is one feed.
    try:
        product_rows = read_rows(folder / "partner_products.csv")
    except FeedError as exc:
        reports.append(Report("partner_products", error=str(exc)))
        product_rows = []
    by_partner: dict[str, list] = {}
    for line, row in product_rows:
        by_partner.setdefault(slug(str(row.get("partner_id") or "")), []).append((line, row))
    for partner_id, rows in sorted(by_partner.items()):
        report = Report(f"partner_products ({partner_id or 'no partner_id'})")
        reports.append(report)
        if partner_id not in partners:
            report.error = f"unknown partner {partner_id!r} in partner_products.csv"
            continue
        import_rows(store, report, "partner_products", rows, normalise_product,
                    {"partner_id": partner_id, "partners": partners},
                    store.read("partner_products", "partner_id", partner_id),
                    dry_run=dry_run, delist_missing=True)

    stored_parts = store.read("parts")
    parts = run("parts", "parts.csv", "parts", normalise_part, {"partners": partners}, stored_parts)
    run("part_compatibility", "part_compatibility.csv", "part_compatibility", normalise_compatibility,
        {"part_ids": set(stored_parts) | set(parts)}, store.read("part_compatibility"))
    return reports


def check_seed(folder: pathlib.Path) -> list[str]:
    """Problems that make the curated seed incomplete (#33), empty when it is fine."""
    problems = []
    partners = {row.get("id") for _, row in read_rows(folder / "partners.csv")}
    tiers = {row.get("partner_tier") for _, row in read_rows(folder / "partners.csv")}
    products = read_rows(folder / "partner_products.csv")
    parts = read_rows(folder / "parts.csv")
    models = {(row.get("brand"), row.get("model")) for _, row in read_rows(folder / "part_compatibility.csv")}
    for label, count, minimum in (
        ("partners", len(partners), SEED_MINIMUMS["partners"]),
        ("partner tiers", len(tiers), SEED_MINIMUMS["partner_tiers"]),
        ("partner products", len(products), SEED_MINIMUMS["partner_products"]),
        ("parts", len(parts), SEED_MINIMUMS["parts"]),
        ("compatible models", len(models), SEED_MINIMUMS["compatible_models"]),
    ):
        if count < minimum:
            problems.append(f"expected at least {minimum} {label}, found {count}")
    return problems
