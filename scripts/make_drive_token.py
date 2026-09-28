#!/usr/bin/env python3
"""Create a narrow-scope Google Drive token AND the folder the pipeline will use.

Why both in one step: the `drive.file` scope only lets this app see files and
folders that the app itself created. A folder made by hand in the Drive website
is invisible to it, so the folder must be created here.

    python scripts/make_drive_token.py client_secret.json --name audiobook

Writes  audiobook_drive_token.json  (paste into *_DRIVE_CREDENTIALS)
and prints the folder ID          (paste into *_DRIVE_ROOT_FOLDER_ID)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create a drive.file token and its root folder")
    parser.add_argument("client_secret", help="OAuth client JSON downloaded from Google Cloud Console")
    parser.add_argument("--name", required=True, help="short label, e.g. audiobook")
    parser.add_argument("--folder-name", help="Drive folder name (default: <name>-automation)")
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

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    # offline + consent forces Google to issue a refresh token every time.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("Google did not return a refresh token. Remove this app at "
              "https://myaccount.google.com/permissions and run again.", file=sys.stderr)
        return 1

    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    folder_name = args.folder_name or f"{args.name}-automation"
    folder = service.files().create(
        body={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}, fields="id,name").execute()
    # Prove the token can see the folder it just made; this is the exact access the pipeline needs.
    service.files().get(fileId=folder["id"], fields="id").execute()

    out = Path(f"{args.name}_drive_token.json")
    out.write_text(json.dumps(json.loads(creds.to_json()), indent=2), encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass  # not supported on every filesystem (e.g. Windows); harmless
    print(f"\nToken saved to: {out}   -> secret {args.name.upper()}_DRIVE_CREDENTIALS (paste the whole file)")
    print(f"Folder created: '{folder['name']}'")
    print(f"Folder ID:      {folder['id']}   -> secret {args.name.upper()}_DRIVE_ROOT_FOLDER_ID")
    print(f"\nKeep {out} private and do NOT commit it (client_secret*.json is already gitignored; "
          f"add *_drive_token.json too).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
