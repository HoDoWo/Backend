"""Print a Firebase ID token for a test user, to scan a preview deployment as that user (#159).

    FIREBASE_WEB_API_KEY=<current_key of google-services.json> \\
    ZAP_USER_EMAIL=zap-a@hodowo.test ZAP_USER_PASSWORD=... \\
      python scripts/security/firebase_token.py

Use dedicated test accounts (never a real user's): create them once in the
Firebase console (Authentication → Add user). The token lasts one hour. The
password is read from the environment only and never printed.
"""

import json
import os
import sys
import urllib.request


def main() -> int:
    key = os.environ["FIREBASE_WEB_API_KEY"]
    body = json.dumps({
        "email": os.environ["ZAP_USER_EMAIL"],
        "password": os.environ["ZAP_USER_PASSWORD"],
        "returnSecureToken": True,
    }).encode()
    request = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={key}",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        print(json.load(response)["idToken"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
