# Kokoro TTS

The project uses the open-weight Kokoro-82M model locally rather than an external TTS API.

Default profiles:

| Profile | Voice | Intended use |
|---|---|---|
| audiobook | `am_adam` | deep, steady narration |
| relaxation | `am_michael` | calm/slow narration |
| motivation | `am_adam` | energetic narration |
| horror | `am_fenrir` | dark/deep narration |
| mystery | `am_onyx` | rich/sophisticated narration |
| documentary | `am_eric` | professional narration |
| storytelling | `bm_fable` | expressive British storytelling |
| hindi | `hm_omega` | Hindi male narration |

The profile is selected with the `AUDIOBOOK_TTS_PROFILE` repository variable. The implementation keeps the Kokoro model loaded per language and synthesizes each chapter independently.

For commercial use, review the license of the exact Kokoro model release and any voice assets used by the deployment.
