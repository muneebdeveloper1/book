# Setup

1. Put each audiobook book folder under `assets/audiobook/books/<Book>/` with `cover.jpg` and `topics.txt`.
2. In Google Drive create `Audiobook Videos`, containing 3-4 subfolders of reusable video clips.
3. Put 2-3 plain thumbnail backgrounds in `assets/audiobook/thumbnail_backgrounds/`.
4. Configure the audiobook Drive root and optional video-library folder ID as GitHub secrets.
5. Run `python scripts/pipeline.py --channel audiobook --test` locally or use the audiobook GitHub workflow.
