# Audiobook architecture

The original audiobook research, rights, Gemini/Groq, Kokoro TTS, checkpoint/Drive persistence, metadata and YouTube idempotency path is retained. The visual stage now uses a reusable Drive video library.

Visual stage:
1. Select one random subfolder from `Audiobook Videos`.
2. Download every supported video clip in that folder to the ephemeral job workspace.
3. Shuffle a fresh deck of clips and repeat decks as needed until narration duration is covered.
4. Trim the final occurrence to the exact narration length.
5. Overlay the fixed perspective book-cover template on every clip.
6. Concatenate without re-encoding the already rendered segments, then mux narration.

Thumbnail stage:
- Choose a random plain background from `assets/audiobook/thumbnail_backgrounds/`.
- Place the straight 2D cover in the fixed left or right box.
- Put the Gemini-generated 3-5 word hook on the opposite side.
