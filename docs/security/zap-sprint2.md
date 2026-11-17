# OWASP ZAP scan of the Sprint 2 API (#159)

## What was scanned

| | |
|---|---|
| Tool | OWASP ZAP 2.16.1, Automation Framework (`scripts/security/zap-sprint2.yaml`) |
| Target | The backend of `main` run on a laptop with an in-memory Firestore (`scripts/security/zap_local_server.py`): real endpoints, validation, auth and rate limits; no Firebase, Stripe, OpenRouter or Kie |
| Routes | All 74 operations of `/openapi.json`: marketplace (search, parts, products, listings, images, cart, checkout, orders, Stripe webhook), conversations and offers, notifications, reviews, forum, manuals |
| Auth | Authenticated scan: every request carries the JWT of test user A |
| Left out of the active scan | The AI endpoints (`/api/chat*`, `/api/panel/*`, listing `scan-label` / `polish` / `estimate`, `environmental-tip`): they only forward to OpenRouter/Kie and an attack would spend credits. Every third-party host is out of scope |
| Policy | Default active-scan policy, strength and threshold medium |
| Date | 17 Nov 2026 |

## Results

The very first run also showed a 500 on `GET /api/manuals`: the in-memory
Firestore of the local server did not know `where(filter=…)`. That was a limit of
the test server, fixed there, and is not in the reports below.

Two scans with the same plan, data and users: **before** on `main` as it was
(commit `11a8872`), **after** on this branch with the fix of `/api/feedback` and
the reviewed false positive marked.

| Risk | Before | After |
|---|---|---|
| High | 1 (Path Traversal, false positive) | 0 |
| Medium | 0 | 0 |
| Low | 2 (`/api/feedback`) | 0 |
| Informational | 1 (User Agent Fuzzer) | 1 |

| Alert | Before | After |
|---|---|---|
| Path Traversal – `/parts/suggest` | High, 1 instance | marked false positive |
| Application Error Disclosure – `POST /api/feedback` | Low, 500 "Internal Server Error" on a non-JSON body | gone: 422 |
| Information Disclosure – Debug Error Messages – `POST /api/feedback` | Low, same request | gone |
| User Agent Fuzzer | Informational | Informational (expected) |

The manual checks give the same result before and after (19/19): the fix
does not change access control.

| Alert | Where | Verdict |
|---|---|---|
| **High – Path Traversal** | `GET /api/marketplace/parts/suggest?error_code=suggest` | **False positive.** ZAP saw the same answer for `error_code=suggest` as for any unknown code (`{"suggested_part": null}`). The endpoint reads an in-memory table and the catalogue, never a file. Marked as false positive in the plan (`alertFilter`). |
| **Low – Application Error Disclosure / Debug Error Messages** | `POST /api/feedback` | **Fixed.** A body that was not JSON made a 500, and the whole body (the user's text) was written to the logs. The body is now validated (`schemas.FeedbackRequest`: invalid → 422) and only its shape is logged. Tests: `tests/test_feedback.py`. |
| Informational – User Agent Fuzzer | all routes | Expected: the API answers the same to every browser. |

The passive scan found no missing security header: the API already sends
`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`
and `Cache-Control: no-store` (`tests/test_security_headers.py`).

Full reports: [before](zap-sprint2-local-2026-11-17-before.html) ·
[after](zap-sprint2-local-2026-11-17-after.html).

## Manual checks

Run with `scripts/security/manual_checks.py` (three test users; A owns a listing, a cart line and a paid order).

| Check | Status | Result |
|---|---|---|
| B reads A's order | 403 | ✅ pass |
| B edits A's listing | 403 | ✅ pass |
| B marks A's listing sold | 403 | ✅ pass |
| B deletes A's listing | 403 | ✅ pass |
| B changes A's cart line | 404 | ✅ pass |
| B removes A's cart line | 404 | ✅ pass |
| B's cart has no line of A | 200 | ✅ pass |
| B's orders have no order of A | 200 | ✅ pass |
| A's listing still A's after the attempts | 200 | ✅ pass |
| C reads the A–B conversation | 404 | ✅ pass |
| Seller cannot accept an offer that does not exist | 409 | ✅ pass |
| Cart without a token | 401 | ✅ pass |
| Orders with a forged (`alg: none`) token | 401 | ✅ pass |
| Stripe webhook without signature | 400 | ✅ pass |
| Stripe webhook with a forged signature | 400 | ✅ pass |
| Upload of a 14 MB photo (limit 5 MB) | 413 | ✅ pass |
| Upload of a non-image | 422 | ✅ pass |
| Listing with 7 photos (limit 6) | 422 | ✅ pass |
| 12 forum posts in a minute (limit 10) | 201 ×10, then 429 | ✅ pass |

## How to run it again

```bash
# 1. The backend with fake data (http://127.0.0.1:8080) and the test users' tokens
python scripts/security/zap_local_server.py

# 2. ZAP (download "Cross Platform" from zaproxy.org, Java 17+)
ZAP_HOME=~/ZAP_2.16.1 ZAP_TARGET=http://127.0.0.1:8080 \
ZAP_TOKEN=$(python3 -c "import json;print(json.load(open('scripts/security/out/tokens.json'))['user-a'])") \
  scripts/security/run_zap.sh

# 3. The manual checks
python scripts/security/manual_checks.py http://127.0.0.1:8080
```

On a **Vercel preview** deployment (never production): set `ZAP_TARGET` to the
preview URL, `ZAP_TOKEN` to the ID token of a Firebase test user
(`scripts/security/firebase_token.py`) and, if the preview is protected,
`ZAP_VERCEL_BYPASS` to the project's "Protection Bypass for Automation" secret.

## Compared with Sprint 1

Sprint 1 (16 and 17 Oct, #44) scanned the web analyser only, without
authentication; its reports are not in the repository yet. Sprint 2 adds the
authenticated API scan of every route and the manual IDOR, webhook, upload and
rate-limit checks.
