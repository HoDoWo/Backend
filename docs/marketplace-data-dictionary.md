# Marketplace Firestore structure

The backend owns partner catalogue writes. Users can create and manage only
their own `listings` documents. Monetary values are stored as integer cents for
stable totals; API requests accept decimal amounts and normalize them.

## Collections

| Collection | Purpose | Writable by |
|---|---|---|
| `partners` | Partner identity, tier, commission and active status | Backend/admin |
| `partner_products` | Official vendor catalogue | Backend/admin |
| `parts` | Replacement-part catalogue and availability | Backend/admin |
| `part_compatibility` | Part-to-model compatibility records | Backend/admin |
| `listings` | User appliance or part listings | Listing owner |
| `carts/{uid}/items` | User cart item snapshots | Cart owner |
| `orders/{uid}/orders` | Purchase records | Backend/payment flow |

## `listings/{listingId}`

Required: `owner_uid`, `listing_kind` (`appliance` or `part`), `item_type`,
`title`, `description`, `category`, `condition`, `price_cents`,
`image_urls`, `handover_methods`, `status`, `created_at`.

Optional: `brand`, `model`, `part_number`, `diagnostic_media_urls`, `pickup_zip`,
`shipping_price_cents`, `quantity`, `sold_at`, `updated_at`.

`status` is `active`, `pending_review`, `sold`, or `removed`. Search returns
only `active` listings. `sponsored`, `partner_tier`, and partner identity fields
are not accepted from user clients.

## Search fields

Catalogue and listing documents should denormalize `title_normalised`,
`brand`, `model`, `category`, `item_type`, `price_cents`, and `created_at`.
The composite indexes in `firestore.indexes.json` support the API filters and
stable ranking pagination.
