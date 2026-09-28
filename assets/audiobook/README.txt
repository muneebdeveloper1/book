Book folders live here:

  assets/audiobook/books/<BOOK NAME>/
      cover.jpg     required - used in scenes and on the thumbnail
      topics.txt    required - one topic per line; '#' starts a comment

queue_state.json and completed_topics.json are created and maintained automatically.
The first book with a pending or in-progress topic is produced.

Optional channel-wide assets in this directory (all skipped with a warning if absent,
unless AUDIOBOOK_STRICT_OPTIONAL_ASSETS=true):

  intro.mp4              prepended to every video
  avatar.mp4             overlaid in a corner
  background.mp4         softlight-blended behind the scenes
  background_music.mp3   ducked under the narration
  thumbnail_template.png 1280x720 template behind the cover and hook text
