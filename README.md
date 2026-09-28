# Audiobook Production

This repository is the audiobook-only production pipeline. It preserves the original Gemini/Groq, TTS, research, rights, Drive persistence, checkpointing, FFmpeg, metadata, thumbnail and YouTube upload architecture while replacing per-video AI scene generation with a reusable Drive video library.

## Production flow

1. Pick a queued audiobook topic.
2. Run the original rights/research/script pipeline.
3. Generate narration with the existing audiobook TTS profile.
4. Randomly select one subfolder from the Drive `Audiobook Videos` library.
5. Download all clips from that folder once for the job.
6. Build a fresh randomized sequence, repeating clips as needed until the narration length is covered. The sequence is not `1,2,3...` and is reshuffled each production.
7. Trim the final occurrence to the exact narration duration.
8. Apply the fixed perspective book-cover overlay to the video.
9. Create a thumbnail from a random plain background with the straight cover on the left or right and a 3-5 word hook on the opposite side.
10. QA, upload and verify on YouTube using the original upload/idempotency logic.

## Run

`python scripts/pipeline.py --channel audiobook --test`

Production requires the same core secrets as the original audiobook channel plus `AUDIOBOOK_VIDEO_LIBRARY_FOLDER_ID` if the Drive library is not named `Audiobook Videos` directly under the configured Drive root.
