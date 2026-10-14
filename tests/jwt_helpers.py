import time

import jwt

TEST_SECRET = "test-secret-that-is-at-least-32-bytes-long"


def make_token(secret: str = TEST_SECRET, algorithm: str = "HS256", **claims) -> str:
    payload = {"sub": "user-123", "exp": int(time.time()) + 3600}
    payload.update(claims)
    # A claim set to None is removed, e.g. make_token(exp=None) -> no expiry.
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, secret, algorithm=algorithm)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}