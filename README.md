# HoDoWo backend

Intermediate server between the HoDoWo Android app and the AI providers
(OpenRouter, and OpenAI / Kie.ai when their integrations are enabled). The app
never talks to those providers directly: this backend holds the API keys and
makes the calls on its behalf.

Built with FastAPI and deployed as a serverless function on Vercel.

## Layout

| Path | Content |
|------|---------|
| `api/index.py` | FastAPI app: `GET /health`, `POST /api/chat`, `POST /api/panel/analyze`, CORS, rate limiting |
| `api/config.py` | Single place that reads environment variables and validates them |
| `api/auth.py` | JWT verification middleware (Bearer tokens) |
| `api/langchain_setup.py` | LangChain agent wiring |
| `api/identify_appliance.py`, `api/manual_retrieve.py` | AI tools |
| `api/panel_analyze.py` | Button and dial detection from a cropped panel photo |
| `api/logging_config.py` | Dev / prod logging |
| `tests/` | Pytest suite |

## Run it locally

Requires Python 3.10+ (the LangChain agent uses `langchain` 1.x).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env        # then fill in OPENROUTER_API_KEY
uvicorn api.index:app --reload
```

The health check is then at <http://localhost:8000/health> and the API docs at
<http://localhost:8000/docs>. Requests to the AI endpoints need a JWT; see
[Authentication](#authentication).

Run the tests with:

```bash
pytest
```

## Environment variables

Secrets are read only from the environment. Never commit `.env` or provider
keys: the repository is public.

| Variable | Required | Purpose |
|----------|----------|---------|
| `OPENROUTER_API_KEY` | yes (for AI calls) | Calls to OpenRouter |
| `OPENROUTER_MODEL` | no | Model id; defaults to `openrouter/free` |
| `KIE_API_KEY` | no | Kie.ai image overlay of the guides; without it the guide is returned without the annotated image |
| `KIE_TIMEOUT_SECONDS` | no | Longest wait for one Kie image; also limited by the request budget |
| `REQUEST_BUDGET_SECONDS` | no | Time budget of one chat request; defaults to `240`, below Vercel's 300 s |
| `FIREBASE_PROJECT_ID`, `FIREBASE_SERVICE_ACCOUNT_JSON` | yes (for saved guides) | Firestore persistence, see below |
| `ALLOWED_ORIGINS` | no | Comma-separated CORS origins; defaults to `http://localhost:3000,https://app.hodowo.com`. Never `*` |
| `AI_RATE_LIMIT` | no | Limit for AI endpoints; defaults to `20/minute` |
| `FORUM_RATE_LIMIT` | no | Limit for forum posts, edits, votes and reports, per user; defaults to `10/minute` |
| `HODOWO_ENV` | no | `dev` (default) or `prod` (JSON logs) |
| `JWT_SECRET` / `JWT_JWKS_URL` | one of them | JWT verification; set exactly one |
| `STRIPE_SECRET_KEY` | no | Stripe **test** secret key (`sk_test_...`); live keys are refused. Without it the checkout is simulated |
| `STRIPE_WEBHOOK_SECRET` | with `STRIPE_SECRET_KEY` | Signing secret of the Stripe webhook (`whsec_...`) |
| `STRIPE_CURRENCY` | no | Defaults to `eur` |

Missing or invalid values fail fast with a clear message from `api/config.py`.

## Endpoints

| Method | Path | Auth | Description |
|-------|------|------|-------------|
| `GET` | `/health` | public | Returns `{"status": "ok"}` |
| `POST` | `/api/chat` | Bearer JWT | Text and/or image question answered by the AI agent |
| `POST` | `/api/chat/stream` | Bearer JWT | Same as `/api/chat`, answered with Server-Sent Events (`step`, `token`, `response`, `error`) |
| `POST` | `/api/panel/analyze` | Bearer JWT | Detects the buttons and dials in a cropped panel photo |
| `POST` | `/api/manuals` | Bearer JWT | Explicitly saves a manual to Firebase Firestore |
| `PATCH` | `/api/manuals/{manual_id}/bookmark` | Bearer JWT | Sets a manual bookmark for its owner |
| `POST` | `/api/manuals/{manual_id}/rate` | Bearer JWT | Toggles or changes a like/dislike rating |
| `GET` | `/api/manuals?limit=&bookmarked=` | Bearer JWT | Newest manuals of the user (Home, Recent Manuals), optionally only the bookmarked ones |
| `GET` | `/api/manuals/popular?limit=` | Bearer JWT | Most searched and saved guides of all users (searches + 2 × saves), without user data |
| `GET` | `/api/appliances/environmental-tip` | Bearer JWT | Daily energy-saving tip from the LLM, the same for the whole day |
| `GET` | `/api/marketplace/cart` | Bearer JWT | The user's cart with current prices, availability and server-side totals |
| `POST` | `/api/marketplace/cart/items` | Bearer JWT | Adds `{product_type, product_id, quantity}`; the same product again increases the quantity |
| `PATCH` | `/api/marketplace/cart/items/{cart_item_id}` | Bearer JWT | Changes the quantity of one of the user's lines |
| `DELETE` | `/api/marketplace/cart/items/{cart_item_id}` | Bearer JWT | Removes one of the user's lines |
| `POST` | `/api/marketplace/checkout` | Bearer JWT | Turns the cart into one pending order per seller and returns the PaymentIntent `client_secret` |
| `POST` | `/api/marketplace/stripe/webhook` | Stripe signature | Confirms or fails the orders of a checkout |
| `GET` | `/api/marketplace/orders` | Bearer JWT | Purchase history of the user, newest first |
| `GET` | `/api/marketplace/orders/{order_id}` | Bearer JWT | One order of the user (`403` for another user's order) |
| `GET` | `/api/marketplace/products?q=&brand=&category=&partner=&cursor=&limit=` | Bearer JWT | Official partner catalogue, paginated, with `partner_tier` and `sponsored` |
| `GET` | `/api/marketplace/products/{product_id}` | Bearer JWT | One partner product |
| `POST` | `/api/marketplace/products` | Partner or admin | Creates a product of the caller's partner |
| `PATCH` | `/api/marketplace/products/{product_id}` | Partner or admin | Changes a product of the caller's partner |

### Marketplace catalogue: seed and partner feeds

The catalogue is never scraped from partner websites. Partners send a feed
file (CSV or JSON) and `scripts/import_partner_feed.py` loads it into
Firestore; the same script loads the curated demo seed in `scripts/seed/`.
It uses the Firebase variables of `.env`.

```bash
python scripts/import_partner_feed.py --seed --dry-run      # what would change
python scripts/import_partner_feed.py --seed                # partners, products, parts, compatibility
python scripts/import_partner_feed.py --feed voltacasa=scripts/feeds/example-voltacasa.json --dry-run
```

- **Feed format** (one row or object per product): `sku`, `title`, `brand`,
  `model`, `category`, `price` (`24.90` or `24,90`) or `price_cents`, `stock`,
  and optionally `description`, `image_url` (https), `sponsored`. A CSV may
  repeat `partner_id`; a JSON file is a list or `{"partner_id": ..., "products": [...]}`.
- **No duplicates**: ids are stable (`partners/{id}`,
  `partner_products/{partner_id}_{sku}`, `parts/{sku}`,
  `part_compatibility/{sku}_{brand}_{model}`); running it twice changes nothing
  and keeps `created_at`.
- **Report**: new / updated (with the changed fields) / unchanged / rejected.
  Invalid rows are skipped with their line number and reason.
- With `--full` the feed is the partner's whole catalogue: its products missing
  from the feed get `stock: 0`. They are never deleted, because orders may
  point at them. Without `--full` a feed only adds and updates products.
- A feed that cannot be read (missing file, bad JSON, unknown partner) is
  reported and the others are still imported; the exit code is then `1`. The
  `--seed` run also exits with `1` if the seed is below the #33 minimums (4
  partners on 2+ tiers, 60 products, 40 parts, 10 compatible models).

The seed data is **fictional demo data**: the partners and the appliance brands
they sell (Aurora, Kelvo, Brezza, Nordwind) are invented. The parts are
"compatible replacements" for 10 real appliance models that users photograph
(for example Bosch WAN28201GB), so the diagnosis can suggest a part; these
compatibility rows are demo data, not manufacturer data.

### Official partner products

Official vendors sell new products. Until the partner dashboard (Sprint 3)
they write through this API, and the dashboard will use the same endpoints.

- **Who can write**: a partner account (Firebase custom claims `role:
  "partner"` and `partner_id`) creates and edits its own partner's products;
  an admin (`role: "admin"`) can do it for any partner and must send
  `partner_id`. Everyone else gets `403`, and a partner gets `403` on another
  partner's product. Only an admin can change `sponsored` (paid placement).
- **Roles** are given with the Admin SDK, never by the app:
  `python scripts/set_user_role.py <email> --partner voltacasa`, `--admin` or
  `--remove`. The user must sign in again to get them.
- **Validation**: `price` between 0.01 and 100000 with at most 2 decimals,
  `stock` a whole number from 0, `category` one of `washing machine`, `dryer`,
  `dishwasher`, `refrigerator`, `oven`, `hob`, `microwave`, `coffee machine`,
  `image_url` https only, SKU unique per partner (case ignored, `409`
  otherwise). Unknown fields are refused, so `partner_tier`, `partner_name`
  and `created_by` cannot be sent: tier and name always come from the
  `partners` document. The SKU is part of the id and cannot be changed.
- **Reading**: the catalogue shows in-stock products of active partners,
  sponsored first, then higher `partner_tier`, then newest. A partner
  filtering on itself (`?partner=<its id>`) also sees its products out of
  stock, to restock them. Products have the same ids as the feed import
  (`{partner_id}_{sku}`), so a feed and the API update the same document.

### Marketplace cart

Every cart call returns the whole updated cart, so the app never computes a
total itself. `product_type` is `new` (partner product), `part` (spare part) or
`used` (second-hand listing). The backend refuses, with `409`, a sold listing,
more items than the catalogue `stock` or the listing `quantity` (default 1),
or a listing of your own. An item that becomes unavailable after being
added stays in the cart with `available: false` and its `unavailable_reason`,
and is left out of `total_cents` and `item_count`. `price_changed` tells the
user that the price is different from when the item was added.

### Second-hand listings

Users publish their own appliances and parts (#35). Every route needs a
signed-in user; changes are for the listing's owner only.

| Method and path | What it does |
|---|---|
| `POST /api/marketplace/listings` | Publishes a listing of the user (`status: active`) |
| `GET /api/marketplace/listings?q=&category=&max_price=&status=&mine=&cursor=&limit=` | Active listings newest first in `results`, paginated with `next_cursor`; `mine=true` lists all the user's listings, sold ones included |
| `GET /api/marketplace/listings/{id}` | One listing; a sold listing only for its owner |
| `PATCH /api/marketplace/listings/{id}` | Edits title, description, condition, price, photos, handover...; a sold listing cannot be edited (`409`) |
| `POST /api/marketplace/listings/{id}/sold` | Marks it as sold: it leaves the marketplace and cannot be bought |
| `DELETE /api/marketplace/listings/{id}` | Deletes it |

Another user's listing answers `403` to edit, sell and delete. Validation
errors are `422` with the field in `detail[].loc`. `condition` is one of
`unopened`, `like-new`, `used-good`, `parts-only`, `refurbished`; the edit
also accepts the labels the app shows ("Like new", "Used, good", "For parts").
Responses carry `condition_label`, `posted_ago` and `is_mine`, never the
owner's uid; marketplace search does not return it either.

### Marketplace checkout (Stripe test mode)

1. The app calls `POST /api/marketplace/checkout`. The backend re-reads every
   cart item from Firestore (price, stock, listing status) and never trusts a
   price sent by the app. Unbuyable items return `409` with the list of
   problems. The cart is split into one order per seller, all paid by a single
   PaymentIntent.
2. The app opens the Stripe PaymentSheet with the returned `client_secret` and
   its publishable key (`pk_test_...`). Card data never reaches this backend.
3. Stripe calls the webhook. Its `Stripe-Signature` header is verified, each
   event is processed once (`stripe_events` collection), and the amount must
   match the orders. On `payment_intent.succeeded` the orders become `paid`,
   second-hand listings become `sold`, stock is decremented and the cart lines
   are removed, in one batch.

Without `STRIPE_SECRET_KEY` the checkout is simulated: orders are recorded as
paid with `simulated: true`. Seller payouts are not part of Sprint 2: every
payment lands on the platform's test account (Stripe Connect in Sprint 3).

Test the webhook locally with the Stripe CLI:

```bash
stripe listen --forward-to localhost:8000/api/marketplace/stripe/webhook
# copy the whsec_... it prints into STRIPE_WEBHOOK_SECRET, then pay with
# 4242 4242 4242 4242 (success), 4000 0000 0000 0002 (declined)
# or 4000 0027 6000 3184 (3D Secure)
```

### Appliance forum

Discussions by appliance category, outside the AI chat (#44). The categories
are a fixed list by appliance type (`washing-machine`, `dryer`, `dishwasher`,
`refrigerator`, `oven`, `hob`, `microwave`, `coffee-machine`, `other`). A
thread can also be about one marketplace product: it is then shown on that
product's detail page too, and its category is the product's.

| Method and path | What it does |
|---|---|
| `GET /api/forum/categories` | The fixed category list |
| `GET /api/forum/categories/{category}/threads?sort=recent\|popular&limit=20&cursor=` | One page of threads with `last_activity_at` and `reply_count`; send `next_cursor` back as `cursor` |
| `GET /api/forum/products/{type}/{id}/threads?sort=popular\|recent` | Questions about one product, each with its most upvoted reply (`top_reply`) |
| `POST /api/forum/threads` | `{"title", "body"?, "category"?, "appliance_model"?, "product_type"?, "product_id"?}`: a category or a product |
| `GET` / `PATCH` / `DELETE /api/forum/threads/{id}` | Read; edit (`title`, `body`) or delete, author only |
| `GET /api/forum/threads/{id}/replies?limit=20&cursor=` | One page of replies, oldest first |
| `POST /api/forum/threads/{id}/replies` | `{"body"}` |
| `PATCH` / `DELETE /api/forum/threads/{id}/replies/{replyId}` | Edit or delete, author only |
| `PUT` / `DELETE .../vote` on a thread or a reply | Add or withdraw your upvote; repeating it is harmless |
| `POST .../report` on a thread or a reply | `{"reason": "spam\|offensive\|unsafe\|off_topic\|other", "note"?}` |
| `POST .../moderation` on a thread or a reply | Admin only: `{"action": "hide\|restore"}` |

Limits: title 8-150 characters, text and replies up to 2000, checked after
trimming; control characters are removed. Text is stored as plain text, so
clients must never render it as HTML. Responses never contain a uid: only
`author_name` (display name from the token, never the email), `author_badge`
and `is_mine`. Badges are set by the backend on threads about a product:
`seller` for the owner of the second-hand listing, `verified_buyer` for a user
with a paid order containing it. Someone else's post answers `404` to edit and
delete, so ids cannot be probed. You cannot upvote or report your own post.

Moderation: every report is stored in `forum_reports` and counted on the post;
3 reports from different users hide it automatically. Admins hide or restore
posts with the moderation endpoints; to make someone an admin run
`python scripts/set_user_role.py <email> --admin` (they must sign in again).

The paginated lists need the forum indexes in `firestore.indexes.json`;
deploy them with `firebase deploy --only firestore` (see "Firebase /
Firestore persistence"). A Community page with recent and popular threads
across categories and "my products" is planned for Sprint 3.

### `POST /api/panel/analyze`

Receives the cropped panel photo and the appliance context, calls the vision
model and returns the controls it can see. It is the data source for the
interactive overlay (S1-15).

- **Body**: `multipart/form-data` with `image` (required), plus the optional
  `brand`, `model` and `language` fields. `language` is a BCP-47 code (for
  example `es`) and controls the language of the labels and descriptions.
- **Response** (`200`):

  ```json
  {
    "controls": [
      {
        "id": "c1",
        "label": "Start/Pause",
        "type": "button",
        "description": "Starts or pauses the selected program",
        "box": {"x": 0.62, "y": 0.40, "w": 0.10, "h": 0.12}
      }
    ]
  }
  ```

  `type` is one of `button`, `dial`, `switch`, `display`, `other`. The box is
  normalised to the image size (0..1), so the client can draw it at any
  resolution; `x, y` is the top-left corner and `w, h` the size. Boxes that
  fall outside the image are clamped, and controls without a label or a valid
  box are dropped. The ids are renumbered to `c1..cN`.

- **Controlled failures**: an image that is not a panel returns
  `{"controls": []}`. A malformed model answer is retried once; if it is still
  unusable the endpoint returns `502` with a clear message. Missing image →
  `422`, non-multipart body → `415`, oversized image → `413`.

## Authentication

`POST /api/chat` requires `Authorization: Bearer <token>`. The middleware
verifies the signature and expiry; set `JWT_SECRET` (HMAC) or `JWT_JWKS_URL`
(asymmetric). `/health` and the docs are public. If no key source is
configured, protected requests fail closed with `500`.

## CORS

Only the browser origins listed in `ALLOWED_ORIGINS` are accepted; a
wildcard is rejected at startup. The Android client does not need CORS (it is
not a browser), but the web app in Sprint 3 will, so the development origin
(`http://localhost:3000`) and the future domain are already configured. A
request from another origin is rejected by the browser.

## Rate limiting

`POST /api/chat` and `POST /api/panel/analyze` are limited per client IP
(default `20/minute`); exceeding it
returns `429`. The counter is kept in process memory, so on Vercel each
instance has its own counter and a burst can still slip through across
instances. This is basic MVP protection; for a hard limit, back the limiter
with Upstash Redis. Forum writes (threads, replies, edits, votes, reports) are
limited per signed-in user instead (default `10/minute`, `FORUM_RATE_LIMIT`). When login exists (S1-07), key the limit by user instead
of IP.

## Deploy on Vercel

1. Import this repository as a Vercel project (root directory = repository root).
2. Add the variables from the table under **Settings → Environment Variables**
   (at least `OPENROUTER_API_KEY`, the JWT source, and `ALLOWED_ORIGINS`).
3. Deploy. Vercel auto-detects the FastAPI app in `api/index.py` and serves
   every route from it (`/health`, `/api/*`); no custom routing is needed.

Production health check:
`https://backend-howdowo.vercel.app/health`.

### Firebase / Firestore persistence

Completed manual guides are saved automatically in the `manuals` Firestore
collection after `/api/chat` or `/api/chat/stream` finishes formatting a manual.
Ratings are stored in `manuals/{manual_id}/ratings/{user_uid}` with one
toggleable rating per user. Configure `FIREBASE_PROJECT_ID` and either
`FIREBASE_SERVICE_ACCOUNT_JSON` (a JSON string) or
`GOOGLE_APPLICATION_CREDENTIALS` (a local service-account file path).

Security rules and indexes live in this repo (`firestore.rules`,
`firestore.indexes.json`) and are not applied by a Vercel deploy. With the
Firebase CLI (`npm install -g firebase-tools`, then `firebase login`), run
from the repository root:

```bash
firebase deploy --only firestore   # rules + indexes, project from .firebaserc
```

New indexes take a few minutes to build; until then the query that needs one
answers an error whose message contains a link to create it.

<img width="4768" height="1947" alt="AdobeExpressPhotos_1cfa9bfd954242a38a89a422d152059c_CopyEdited" src="https://github.com/user-attachments/assets/9ace2589-7f66-47de-823f-390001cc3635" />

### Serverless execution time (risk 7)

The project is on the Vercel Hobby plan, where a function can run for at most
300 seconds (Pro: up to 800 s). Every chat request therefore has a time budget
(`REQUEST_BUDGET_SECONDS`, 240 s by default, see `api/time_budget.py`): the
Kie image overlay is skipped when less than 45 s are left and every external
call has a timeout, so the answer always arrives before Vercel stops the
function. When a request still times out, the Vercel log shows
`Task timed out after 300 seconds` and the app receives a closed connection.

## Security

- No API keys are stored in the repository; they live only in the environment.
- `.env` files are ignored by Git, `.env.example` documents the names only.
