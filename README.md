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
| `HODOWO_ENV` | no | `dev` (default) or `prod` (JSON logs) |
| `JWT_SECRET` / `JWT_JWKS_URL` | one of them | JWT verification; set exactly one |

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
with Upstash Redis. When login exists (S1-07), key the limit by user instead
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
