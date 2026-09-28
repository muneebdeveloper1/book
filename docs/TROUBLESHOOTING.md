# Troubleshooting

- `Drive video library folder not found`: set `AUDIOBOOK_VIDEO_LIBRARY_FOLDER_ID` or place the named `Audiobook Videos` folder directly under the audiobook Drive root.
- `contains no supported video clips`: put `.mp4`, `.mov`, `.m4v`, `.mkv`, or `.webm` clips inside the selected subfolder.
- `No audiobook thumbnail background`: add 2-3 JPG/PNG backgrounds to `assets/audiobook/thumbnail_backgrounds/`.
- If a job resumes on a fresh runner, the selected library folder is restored from `video_library.json` and its clips are downloaded again.
