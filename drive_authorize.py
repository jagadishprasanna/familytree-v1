"""One-time Google sign-in so the Time Capsule can add and delete photos in YOUR Drive folder.

Run this on your own computer (it opens a browser), not on Render:

    pip install google-auth-oauthlib
    python drive_authorize.py client_secret.json
        (or: python drive_authorize.py <client-id> <client-secret>)

It prints three values. Add them in Render under Environment, then redeploy:
    DRIVE_OAUTH_CLIENT_ID, DRIVE_OAUTH_CLIENT_SECRET, DRIVE_OAUTH_REFRESH_TOKEN

Before running it, in Google Cloud Console (the same project that holds your service account):
  1. APIs & Services > OAuth consent screen: User type External, fill in the app name and your email,
     add yourself as a test user, then press "Publish app" so the status is "In production".
     (In "Testing" status Google makes the token expire after 7 days. An unverified personal app is fine:
     when you sign in, click Advanced > Go to <app name>.)
  2. APIs & Services > Credentials > Create credentials > OAuth client ID > Application type: Desktop app.
     Download the JSON (client_secret_....json), or copy the client id and secret.
Sign in with the Google account that owns the Time Capsule folder.
"""
import json
import sys

SCOPE = "https://www.googleapis.com/auth/drive"


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install it first:  pip install google-auth-oauthlib")
        return 1
    if len(argv) == 1:
        with open(argv[0], encoding="utf-8") as f:
            cfg = json.load(f)
        cfg = cfg.get("installed") or cfg.get("web") or cfg
        cid, secret = cfg["client_id"], cfg["client_secret"]
    else:
        cid, secret = argv
    flow = InstalledAppFlow.from_client_config(
        {"installed": {"client_id": cid, "client_secret": secret,
                       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                       "token_uri": "https://oauth2.googleapis.com/token",
                       "redirect_uris": ["http://localhost"]}}, scopes=[SCOPE])
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google did not return a refresh token. Remove this app at "
              "https://myaccount.google.com/permissions and run the script again.")
        return 1
    print("\nAdd these three variables in Render (Environment), then redeploy:\n")
    print(f"DRIVE_OAUTH_CLIENT_ID={cid}")
    print(f"DRIVE_OAUTH_CLIENT_SECRET={secret}")
    print(f"DRIVE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nKeep these private (like a password). Never commit them to GitHub.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))