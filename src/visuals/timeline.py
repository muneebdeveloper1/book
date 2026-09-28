from pathlib import Path
from src.rendering.ffmpeg import media_info
from src.utils.io import write_json


def build(audio_path, scenes, out):
    dur = float(media_info(audio_path)['format']['duration'])
    if not scenes:
        raise RuntimeError('No visual scenes available')

    # Chapter-aware timing first; fall back to weighted equal distribution.
    chapter_groups = {}
    for s in scenes:
        chapter_groups.setdefault(str(s.get('chapter', '1')), []).append(s)

    chapters = list(chapter_groups)
    weights = {c: max(1, len(chapter_groups[c])) for c in chapters}
    total_weight = sum(weights.values())
    chapter_cursor = 0.0
    timeline = []

    for c in chapters:
        chapter_duration = dur * weights[c] / total_weight
        group = chapter_groups[c]
        step = chapter_duration / len(group)
        for i, scene in enumerate(group):
            start = chapter_cursor + i * step
            end = chapter_cursor + (i + 1) * step
            timeline.append({
                'start': round(start, 3),
                'end': round(end if i < len(group)-1 else chapter_cursor + chapter_duration, 3),
                'asset': scene['asset'],
                'scene_id': scene['scene_id'],
                'chapter': scene.get('chapter'),
                'media_type': scene.get('media_type', 'image'),
                'provider': scene.get('provider'),
                'selection_score': scene.get('selection_score'),
            })
        chapter_cursor += chapter_duration

    if timeline:
        timeline[-1]['end'] = round(dur, 3)
    write_json(out, timeline)
    return timeline
