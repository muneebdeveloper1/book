"""YouTube upload with strict idempotency.

The failure this design exists to prevent: the video uploads, a later step
(thumbnail, verification) fails, the job retries and uploads a SECOND copy.

Rules enforced here:

* every upload carries a unique production marker in its description;
* the caller receives the video id through ``on_video_id`` the instant the
  insert returns - before the thumbnail is touched - so it is persisted even if
  everything after it fails;
* before uploading, ``find_existing`` looks for the marker; if that lookup is
  unavailable we raise ``UploadAmbiguous`` instead of guessing;
* a thumbnail failure never invalidates an uploaded video.

Quota costs are metered against the job budget (insert is ~1600 units of a
10,000/day default quota, so two failed blind retries can end a day).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Mapping

from src.domain.models import YouTubeState
from src.errors import AuthError, ConfigError, PipelineError, RetryableError, UploadAmbiguous, classify_exception
from src.utils.log import get_logger

log = get_logger("youtube")

COST_INSERT = 1600
COST_SEARCH = 100
COST_THUMBNAIL = 50
COST_LIST = 1
MAX_DESCRIPTION = 4800


class YouTube:
    def __init__(self, token_env: str, settings, budget, env: Mapping[str, str] | None = None, service=None):
        import os

        env = os.environ if env is None else env
        self.token_env = token_env
        self.settings = settings
        self.budget = budget
        if service is not None:
            self.svc = service
            return
        raw = (env.get(token_env) or "").strip()
        if not raw:
            raise ConfigError(f"Missing YouTube credential: {token_env}")
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{token_env} is not valid JSON") from exc
        if info.get("type") not in (None, "authorized_user") or not all(
                info.get(field) for field in ("client_id", "client_secret", "refresh_token")):
            raise ConfigError(f"{token_env} must hold Google OAuth authorized-user credentials "
                              "(client_id, client_secret, refresh_token)")
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        self.svc = build("youtube", "v3", credentials=Credentials.from_authorized_user_info(info),
                         cache_discovery=False)

    # ------------------------------------------------------------- internals
    def _execute(self, request, label: str, cost: int, attempts: int = 5):
        self.budget.spend("youtube_units", cost)
        last: PipelineError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - classified immediately
                classified = classify_exception(exc)
                if classified is None:
                    raise
                if not classified.retryable:
                    raise classified from exc
                last = classified
                if attempt < attempts:
                    delay = min(60, 2 ** attempt)
                    log.warning("youtube retry", op=label, attempt=attempt, delay=delay)
                    time.sleep(delay)
        raise RetryableError(f"{label} failed after {attempts} attempts: {last}") from last

    @staticmethod
    def marker_line(job_id: str) -> str:
        return f"Production ID: {job_id}"

    # ---------------------------------------------------------------- lookup
    def find_existing(self, job_id: str, strict: bool = True) -> str | None:
        """Find a previous upload of this job by its production marker.

        ``strict`` raises ``UploadAmbiguous`` when the lookup itself fails, so
        the caller never re-uploads on the strength of a failed search.
        """
        if not job_id:
            return None
        try:
            response = self._execute(
                self.svc.search().list(part="id", q=self.marker_line(job_id), forMine=True, type="video",
                                       maxResults=10),
                "existing upload search", COST_SEARCH, attempts=3)
        except PipelineError as exc:
            if strict:
                raise UploadAmbiguous(
                    f"Cannot confirm whether job {job_id} was already uploaded: {exc}. "
                    "Refusing to upload again; retry once the API is reachable.") from exc
            log.warning("existing-upload search unavailable", error=str(exc)[:160])
            return None
        for item in response.get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            if video_id and self._marker_matches(video_id, job_id):
                return video_id
        return None

    def _marker_matches(self, video_id: str, job_id: str) -> bool:
        """Search is fuzzy, so confirm the marker really is in the description."""
        try:
            response = self._execute(self.svc.videos().list(part="snippet", id=video_id),
                                     "marker confirmation", COST_LIST, attempts=2)
        except PipelineError:
            return False
        for item in response.get("items", []):
            if self.marker_line(job_id) in (item.get("snippet", {}).get("description") or ""):
                return True
        return False

    # ---------------------------------------------------------------- upload
    def upload(self, video, meta: dict, job_id: str, visibility: str = "private",
               on_video_id: Callable[[str], None] | None = None) -> str:
        from googleapiclient.http import MediaFileUpload

        video = Path(video)
        if not video.is_file():
            raise ConfigError(f"Video file not found: {video}")
        description = (meta.get("description") or "").strip()
        marker = self.marker_line(job_id)
        description = f"{description}\n\n{marker}".strip()[:MAX_DESCRIPTION]
        body = {
            "snippet": {
                "title": (meta.get("title") or job_id)[:100],
                "description": description,
                "tags": list(meta.get("keywords") or [])[:60],
                "categoryId": str(self.settings.youtube.category_id),
            },
            "status": {"privacyStatus": visibility, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
        request = self.svc.videos().insert(part="snippet,status", body=body, media_body=media)
        self.budget.spend("youtube_units", COST_INSERT)
        response, failures = None, 0
        while response is None:
            try:
                _, response = request.next_chunk(num_retries=3)
            except Exception as exc:  # noqa: BLE001 - classified immediately
                classified = classify_exception(exc)
                failures += 1
                if classified is None or not classified.retryable:
                    raise (classified or exc) from exc
                if failures >= 5:
                    # The bytes may or may not have landed; the caller must resolve
                    # this by marker lookup rather than uploading again.
                    raise UploadAmbiguous(f"Upload of {job_id} was interrupted: {classified}") from exc
                time.sleep(min(60, 2 ** failures))
        video_id = response.get("id")
        if not video_id:
            raise RetryableError(f"YouTube accepted the upload but returned no id: {response}")
        log.info("video uploaded", video_id=video_id, visibility=visibility)
        if on_video_id:
            # Persist BEFORE anything else can fail: a thumbnail or verification
            # error must never cause a duplicate upload on the next run.
            on_video_id(video_id)
        return video_id

    def set_thumbnail(self, video_id: str, thumbnail) -> None:
        from googleapiclient.http import MediaFileUpload

        thumbnail = Path(thumbnail)
        if not thumbnail.is_file():
            raise ConfigError(f"Thumbnail not found: {thumbnail}")
        self._execute(self.svc.thumbnails().set(videoId=video_id,
                                                media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")),
                      "thumbnail upload", COST_THUMBNAIL, attempts=3)
        log.info("thumbnail set", video_id=video_id)

    def verify(self, video_id: str) -> dict | None:
        response = self._execute(self.svc.videos().list(part="snippet,status,contentDetails", id=video_id),
                                 "upload verification", COST_LIST)
        items = response.get("items", [])
        return items[0] if items else None


def publish(yt: YouTube, job_id: str, video, thumbnail, meta: dict, visibility: str,
            state_path, save: Callable[[dict], None], existing: dict | None = None) -> YouTubeState:
    """Upload (or adopt an existing upload), then attach the thumbnail and verify.

    Ordering is the whole point: the video id is saved the moment it exists, so
    each later failure is recoverable without a second upload.
    """
    state = YouTubeState.model_validate(existing) if existing else None
    if state is None or not state.video_id:
        found = yt.find_existing(job_id, strict=True)
        if found:
            log.warning("adopting an existing upload for this job", video_id=found)
            state = YouTubeState(job_id=job_id, video_id=found, credential_env=yt.token_env, adopted_existing=True)
            save(state.model_dump())
        else:
            holder: dict[str, str] = {}

            def remember(video_id: str) -> None:
                holder["id"] = video_id
                save(YouTubeState(job_id=job_id, video_id=video_id, credential_env=yt.token_env,
                                  uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())).model_dump())

            video_id = yt.upload(video, meta, job_id, visibility, on_video_id=remember)
            state = YouTubeState(job_id=job_id, video_id=video_id, credential_env=yt.token_env,
                                 uploaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    if state.thumbnail in ("pending", "failed"):
        if thumbnail and Path(thumbnail).is_file():
            try:
                yt.set_thumbnail(state.video_id, thumbnail)
                state.thumbnail = "set"
            except PipelineError as exc:
                # Recorded, not fatal: the next run retries the thumbnail alone.
                state.thumbnail = "failed"
                state.thumbnail_error = str(exc)[:500]
                if yt.settings.youtube.thumbnail_required:
                    save(state.model_dump())
                    raise
                log.warning("thumbnail failed; video stays published", error=str(exc)[:200])
        else:
            state.thumbnail = "skipped"
        save(state.model_dump())

    details = yt.verify(state.video_id)
    state.verified = bool(details)
    save(state.model_dump())
    if not details:
        raise RetryableError(f"YouTube did not return details for {state.video_id} yet")
    return state
