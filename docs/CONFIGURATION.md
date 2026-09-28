# Configuration

Active configuration is `config/channels.yaml`.

Important audiobook settings:
- `AUDIOBOOK_VIDEO_LIBRARY_FOLDER_ID`: optional Drive ID of the main reusable video library folder.
- `AUDIOBOOK_VIDEO_LIBRARY_FOLDER_NAME`: fallback name when the ID is not supplied.
- `AUDIOBOOK_TTS_PROFILE`: existing audiobook Kokoro profile.
- `AUDIOBOOK_TARGET_MINUTES`: narration target.
- `AUDIOBOOK_THUMBNAIL_COVER_SIDE`: `random`, `left`, or `right`.

Add 2-3 plain thumbnail backgrounds to `assets/audiobook/thumbnail_backgrounds/`.
