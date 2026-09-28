from pathlib import Path

from src.visuals.video_library import random_sequence


def test_random_sequence_covers_audio_and_repeats_non_sequentially(monkeypatch, tmp_path):
    clips=[tmp_path/f"{i}.mp4" for i in range(1,6)]
    durations={p:4.0 for p in clips}
    monkeypatch.setattr("src.visuals.video_library.duration_of", lambda p: durations[Path(p)])
    seq=random_sequence(clips, 31.0, seed=7)
    assert abs(sum(x["duration"] for x in seq)-31.0)<1e-6
    assert len(seq)>5
    names=[x["asset"] for x in seq]
    assert names[:5] != [p.name for p in clips]
    assert all(a != b for a,b in zip(names,names[1:]))
    assert seq[-1]["duration"] == 3.0
