# HoDoWo backend

Intermediate server between the HoDoWo Android app and the AI providers
(OpenRouter, and OpenAI / Kie.ai when their integrations are enabled). The app
never talks to those providers directly: this backend holds the API keys and
makes the calls on its behalf.

Built with FastAPI and deployed as a serverless function on Vercel.

## Layout

| Path | Content |
|------|---------|
| `api/index.py` | FastAPI app: `GET /health`, `POST /api/chat`, CORS, rate limiting |
| `api/config.py` | Single place that reads environment variables and validates them |
| `api/auth.py` | JWT verification middleware (Bearer tokens) |
| `api/langchain_setup.py` | LangChain agent wiring |
| `api/identify_appliance.py`, `api/manual_retrieve.py` | AI tools |
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
| `OPENAI_API_KEY`, `KIE_API_KEY` | no | Future providers |
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

`POST /api/chat` is limited per client IP (default `20/minute`); exceeding it
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
`https://<your-vercel-project>.vercel.app/health` (replace with the real URL).

### Serverless execution time (risk 7)

Vercel Functions have a maximum execution time that depends on the plan.
Vision and image-generation calls can take a while, so check the current limit
in the Vercel docs and verify it with a real LLM call in the walking skeleton
before relying on it.

## Security

- No API keys are stored in the repository; they live only in the environment.
- `.env` files are ignored by Git, `.env.example` documents the names only.
