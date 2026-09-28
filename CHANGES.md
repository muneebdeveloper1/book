# Changes

## Audiobook-only production

- Repository is now dedicated to the audiobook channel; the second channel and its workflows, configuration, discovery, stock providers and tests were removed.
- Preserved the original audiobook rights, research, Gemini/Groq routing, Kokoro TTS, Drive checkpointing and YouTube upload/idempotency path.
- Replaced per-audiobook AI image generation with a reusable Google Drive video library.
- Each production randomly selects one library subfolder, downloads all clips, creates a fresh randomized repeat sequence, and trims the final occurrence to the narration duration.
- Added a fixed 1920x1080 perspective book-cover overlay based on the supplied reference placement.
- Added audiobook thumbnail backgrounds, straight cover placement on left/right, and a 3-5 word hook opposite the cover.
