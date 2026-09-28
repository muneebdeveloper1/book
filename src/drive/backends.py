"""Storage backends for ``DriveStore``.

``GoogleBackend`` talks to Google Drive v3. ``MemoryBackend`` is a faithful
in-memory stand-in (including md5 checksums and creation order) used by tests
and by local dry-runs, with hooks to inject failures at exact points.
"""
from __future__ import annotations

import hashlib
import itertools
import os
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from src.errors import AuthError, ConfigError, PipelineError, RetryableError, classify_exception
from src.utils.log import get_logger

log = get_logger("drive")
FOLDER = "application/vnd.google-apps.folder"
FULL_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass
class Entry:
    id: str
    name: str
    is_folder: bool
    size: int = 0
    md5: str = ""
    created: str = ""


class Backend(Protocol):
    root_id: str

    def list_children(self, parent_id: str) -> list[Entry]: ...
    def create_folder(self, name: str, parent_id: str) -> str: ...
    def upload(self, local_path: Path, parent_id: str, name: str, existing_id: str | None = None) -> Entry: ...
    def download(self, file_id: str, local_path: Path) -> None: ...
    def delete(self, file_id: str) -> None: ...


class MemoryBackend:
    """In-memory Drive. ``fail_hook(op, name)`` may raise to simulate outages."""

    def __init__(self, fail_hook: Callable[[str, str], None] | None = None):
        self.root_id = "root"
        self._counter = itertools.count(1)
        self.nodes: dict[str, dict] = {"root": {"name": "root", "parent": None, "folder": True, "data": b"", "created": 0}}
        self.fail_hook = fail_hook
        self.uploads: list[str] = []
        self.bytes_uploaded = 0

    def _hook(self, op: str, name: str) -> None:
        if self.fail_hook:
            self.fail_hook(op, name)

    def list_children(self, parent_id: str) -> list[Entry]:
        self._hook("list", parent_id)
        out = []
        for nid, n in self.nodes.items():
            if n["parent"] == parent_id:
                out.append(Entry(nid, n["name"], n["folder"], len(n["data"]),
                                 "" if n["folder"] else hashlib.md5(n["data"]).hexdigest(), f"{n['created']:012d}"))
        return sorted(out, key=lambda e: e.created)

    def create_folder(self, name: str, parent_id: str) -> str:
        self._hook("create_folder", name)
        nid = f"n{next(self._counter)}"
        self.nodes[nid] = {"name": name, "parent": parent_id, "folder": True, "data": b"", "created": int(nid[1:])}
        return nid

    def upload(self, local_path: Path, parent_id: str, name: str, existing_id: str | None = None) -> Entry:
        self._hook("upload", name)
        data = Path(local_path).read_bytes()
        if existing_id:
            self.nodes[existing_id]["data"] = data
            nid = existing_id
        else:
            nid = f"n{next(self._counter)}"
            self.nodes[nid] = {"name": name, "parent": parent_id, "folder": False, "data": data, "created": int(nid[1:])}
        self.uploads.append(name)
        self.bytes_uploaded += len(data)
        n = self.nodes[nid]
        return Entry(nid, n["name"], False, len(data), hashlib.md5(data).hexdigest(), f"{n['created']:012d}")

    def download(self, file_id: str, local_path: Path) -> None:
        self._hook("download", self.nodes[file_id]["name"])
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.nodes[file_id]["data"])

    def delete(self, file_id: str) -> None:
        self._hook("delete", file_id)
        for cid in [k for k, v in self.nodes.items() if v["parent"] == file_id]:
            self.delete(cid)
        self.nodes.pop(file_id, None)


class GoogleBackend:
    """Google Drive v3 backend with paging, classified errors and retries."""

    def __init__(self, credentials_json: str, root_folder_id: str, require_narrow_scope: bool = False):
        import json

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        try:
            info = json.loads(credentials_json)
        except json.JSONDecodeError as exc:
            raise ConfigError("Drive credentials are not valid JSON") from exc
        creds = self._credentials(info, Credentials)
        scopes = set(info.get("scopes") or getattr(creds, "scopes", None) or [])
        if FULL_DRIVE_SCOPE in scopes:
            msg = ("Drive token carries the FULL Drive scope. Use scripts/make_drive_token.py to "
                   "create a 'drive.file' token so a leaked secret cannot read your whole Drive.")
            if require_narrow_scope:
                raise ConfigError(msg)
            log.warning(msg)
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.root_id = root_folder_id

    @staticmethod
    def _credentials(info: dict, Credentials):
        kind = info.get("type")
        if kind == "service_account":
            raise ConfigError("Personal Drive needs OAuth authorized-user JSON, not a service account.")
        if "installed" in info or "web" in info:
            raise ConfigError("Drive secret is an OAuth *client* file; run scripts/make_drive_token.py to get a token.")
        need = [f for f in ("client_id", "client_secret", "refresh_token") if not info.get(f)]
        if need:
            raise ConfigError("Drive credentials missing fields: " + ", ".join(need))
        return Credentials.from_authorized_user_info(info)

    @staticmethod
    def _call(request, label: str, attempts: int = 5):
        last = None
        for attempt in range(1, attempts + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - classified right here
                classified = classify_exception(exc)
                if classified is None:
                    raise
                if not classified.retryable:
                    raise classified from exc
                last = classified
                if attempt < attempts:
                    delay = min(30, 2 ** attempt)
                    log.warning("drive retry", op=label, attempt=attempt, delay=delay)
                    time.sleep(delay)
        raise RetryableError(f"{label} failed after {attempts} attempts: {last}") from last

    def list_children(self, parent_id: str) -> list[Entry]:
        out, token = [], None
        while True:
            resp = self._call(self.svc.files().list(
                q=f"'{parent_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,size,md5Checksum,createdTime)",
                pageSize=1000, pageToken=token, orderBy="createdTime",
                includeItemsFromAllDrives=True, supportsAllDrives=True), "list")
            for f in resp.get("files", []):
                out.append(Entry(f["id"], f["name"], f["mimeType"] == FOLDER, int(f.get("size") or 0),
                                 f.get("md5Checksum", ""), f.get("createdTime", "")))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def create_folder(self, name: str, parent_id: str) -> str:
        return self._call(self.svc.files().create(
            body={"name": name, "mimeType": FOLDER, "parents": [parent_id]},
            fields="id", supportsAllDrives=True), "create folder")["id"]

    def upload(self, local_path: Path, parent_id: str, name: str, existing_id: str | None = None) -> Entry:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(local_path), resumable=True, chunksize=8 * 1024 * 1024)
        fields = "id,name,mimeType,size,md5Checksum,createdTime"
        if existing_id:
            request = self.svc.files().update(fileId=existing_id, media_body=media, fields=fields, supportsAllDrives=True)
        else:
            request = self.svc.files().create(body={"name": name, "parents": [parent_id]}, media_body=media,
                                              fields=fields, supportsAllDrives=True)
        response, failures = None, 0
        while response is None:
            try:
                _, response = request.next_chunk(num_retries=3)
            except Exception as exc:  # noqa: BLE001 - classified right here
                classified = classify_exception(exc)
                failures += 1
                if classified is None or not classified.retryable or failures >= 5:
                    raise (classified or exc) from exc
                time.sleep(min(30, 2 ** failures))
        return Entry(response["id"], response["name"], False, int(response.get("size") or 0),
                     response.get("md5Checksum", ""), response.get("createdTime", ""))

    def download(self, file_id: str, local_path: Path) -> None:
        from googleapiclient.http import MediaIoBaseDownload

        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_name(local_path.name + ".part")
        try:
            with tmp.open("wb") as out:
                downloader = MediaIoBaseDownload(out, self.svc.files().get_media(fileId=file_id, supportsAllDrives=True))
                done = False
                while not done:
                    try:
                        _, done = downloader.next_chunk(num_retries=3)
                    except (ssl.SSLError, ConnectionError, TimeoutError, OSError) as exc:
                        raise RetryableError(f"Drive download failed for {file_id}: {exc}") from exc
            tmp.replace(local_path)
        finally:
            tmp.unlink(missing_ok=True)

    def delete(self, file_id: str) -> None:
        self._call(self.svc.files().delete(fileId=file_id, supportsAllDrives=True), "delete")
