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
| `carts/{lineId}` | One cart line per document (`uid`, `product_type`, `product_id`, `quantity`, `price_cents`, `added_at`); id `{uid}_{product_type}_{product_id}` | Backend (cart API); owner can read |
| `orders/{orderId}` | One order per seller with the items embedded; `uid`, `seller_id`, `checkout_id`, `status`, `total_cents`, `payment_intent_id`, `simulated`, `created_at`, `paid_at` | Backend (checkout and Stripe webhook); owner can read |
| `stripe_events/{eventId}` | Stripe webhook events already processed | Backend only |

## `listings/{listingId}`

Required: `owner_uid`, `listing_kind` (`appliance` or `part`), `item_type`,
`title`, `description`, `category`, `condition`, `price_cents`,
`image_urls`, `handover_methods`, `status`, `created_at`.

Optional: `brand`, `model`, `part_number`, `diagnostic_media_urls`, `pickup_zip`,
`shipping_price_cents`, `quantity`, `sold_at`, `updated_at`.

`item_type` is always `used` for user listings: the cart and the checkout use it
(`product_type: "used"`) to find the item in `listings`. `listing_kind` says
whether it is a part or an appliance. `quantity` (default 1) is how many
identical items are offered; each paid order decrements it and the listing
becomes `sold` at 0. Dates are epoch milliseconds. `seller_name` is the
user's display name or "Community seller", never the email.

`status` is `active`, `pending_review`, `sold`, or `removed`. Search returns
only `active` listings. `sponsored`, `partner_tier`, and partner identity fields
are not accepted from user clients.

## Search fields

Catalogue and listing documents should denormalize `title_normalised`,
`brand`, `model`, `category`, `item_type`, `price_cents`, and `created_at`.
Catalogue documents use `stock` (buyable when `stock > 0`); the older
`available` flag is only read when `stock` is missing. `rating` and
`review_count` are returned only when real reviews exist, otherwise `null`.
The composite indexes in `firestore.indexes.json` support the API filters and
stable ranking pagination.
