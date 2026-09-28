#!/usr/bin/env python3
"""Create the YouTube token for ONE channel.

    python scripts/make_youtube_token.py client_secret.json --name audiobook

Run it once per channel. When the browser opens, sign in and PICK THE YOUTUBE
CHANNEL you want this token to upload to; the script prints the channel it got
so you can confirm before saving anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# `youtube` covers upload + thumbnails; force-ssl is needed by the search call
# that detects an earlier upload of the same job (idempotency check).
SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create a YouTube OAuth token for one channel")
    parser.add_argument("client_secret", help="OAuth client JSON downloaded from Google Cloud Console")
    parser.add_argument("--name", required=True, help="short label, e.g. books")
    args = parser.parse_args(argv)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print("Missing packages. Run: pip install google-auth-oauthlib google-api-python-client", file=sys.stderr)
        return 1
    secret = Path(args.client_secret)
    if not secret.is_file():
        print(f"File not found: {secret}", file=sys.stderr)
        return 1

    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(
        port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google did not return a refresh token. Remove this app at "
              "https://myaccount.google.com/permissions and run again.", file=sys.stderr)
        return 1

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = youtube.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        print("This Google account has no YouTube channel. Create one first, then run again.", file=sys.stderr)
        return 1
    title = items[0]["snippet"]["title"]
    print(f"\nAuthorised channel: '{title}'")
    if input("Is this the right channel? [y/N] ").strip().lower() != "y":
        print("Nothing saved. Run again and choose the correct channel in the browser.")
        return 1

    out = Path(f"{args.name}_youtube_token.json")
    out.write_text(json.dumps(json.loads(creds.to_json()), indent=2), encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    secret_name = {"audiobook": "YOUTUBE_TOKEN_JSON_FINANCE", "books": "YOUTUBE_TOKEN_JSON_BOOKS",
                   "audiobook": "YOUTUBE_TOKEN_JSON_BOOKS"}.get(args.name, "YOUTUBE_TOKEN_JSON_<CHANNEL>")
    print(f"Token saved to: {out}   -> secret {secret_name} (paste the whole file)")
    print("Keep it private and do NOT commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
