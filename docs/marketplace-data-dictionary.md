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
| `forum_threads/{threadId}` | A forum thread: `category`, `title`, `body`, `appliance_model`, optional `product_type` + `product_id` + `product_key` (`{type}:{id}`) + `product_title`, `uid`, `author_name`, `author_badge`, `status`, `upvotes`, `reply_count`, `report_count`, `created_at`, `edited_at`, `last_activity_at` | Backend (forum API) only |
| `forum_threads/{threadId}/replies/{replyId}` | A reply: `uid`, `author_name`, `author_badge`, `body`, `status`, `upvotes`, `report_count`, `created_at`, `edited_at` | Backend (forum API) only |
| `forum_votes/{voteId}` | One upvote; id `{uid}_{threadId}` or `{uid}_{threadId}_{replyId}`, so a user votes once | Backend only |
| `forum_reports/{reportId}` | One report (`reason`, `note`, `created_at`); same id scheme as votes | Backend only |

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

## Forum

`category` is one of the fixed ids in `forum_service.CATEGORIES`; a thread
about a product takes the product's category (`other` when it has none of
them). `status` is `visible`, `hidden` (3 reports from different users, or an
admin) or `deleted` (by its author); only `visible` posts are returned.
`author_badge` is `seller`, `verified_buyer` or `null` and is decided by the
backend when the post is written. Category lists and replies are paginated
Firestore queries that need the forum indexes in `firestore.indexes.json`;
the threads of one product are read with one equality filter on `product_key`
and sorted in memory. The forum collections are closed to clients in
`firestore.rules`: the app reads them through the API, which never returns
uids.
