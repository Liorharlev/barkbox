from pathlib import Path

from barkbox.clips import count_tagged, load_clips, load_tags, resolve_clips


def _make_sounds(tmp_path, names):
    for n in names:
        (tmp_path / n).write_bytes(b"\x00")
    return tmp_path


def test_load_clips_filters_and_sorts(tmp_path):
    _make_sounds(tmp_path, ["b.mp3", "a.wav", "notes.txt", "c.MP3"])
    got = [p.name for p in load_clips(tmp_path)]
    assert got == ["a.wav", "b.mp3", "c.MP3"]


def test_load_clips_missing_dir(tmp_path):
    assert load_clips(tmp_path / "nope") == []


def test_load_tags_missing_or_empty(tmp_path):
    assert load_tags(tmp_path / "nope.yaml") == {}
    empty = tmp_path / "tags.yaml"
    empty.write_text("# just a comment\n", encoding="utf-8")
    assert load_tags(empty) == {}


def test_load_tags_normalises_scalar_to_list(tmp_path):
    p = tmp_path / "tags.yaml"
    p.write_text("bark_01.mp3: alert\nbark_02.mp3: [chase, response]\n", encoding="utf-8")
    assert load_tags(p) == {"bark_01.mp3": ["alert"], "bark_02.mp3": ["chase", "response"]}


def test_resolve_clips_prefers_tagged():
    clips = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
    tags = {"a.mp3": ["alert"], "b.mp3": ["chase"]}
    assert resolve_clips(clips, tags, ["alert"]) == [Path("a.mp3")]


def test_resolve_clips_falls_back_to_untagged_pool():
    clips = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
    tags = {"a.mp3": ["alert"]}
    # no clip tagged 'chase' -> untagged pool (b, c)
    assert resolve_clips(clips, tags, ["chase"]) == [Path("b.mp3"), Path("c.mp3")]


def test_resolve_clips_falls_back_to_everything():
    clips = [Path("a.mp3"), Path("b.mp3")]
    tags = {"a.mp3": ["alert"], "b.mp3": ["response"]}
    # nothing tagged 'chase' and nothing untagged -> all clips
    assert resolve_clips(clips, tags, ["chase"]) == clips


def test_resolve_clips_no_tags_at_all_uses_everything():
    clips = [Path("a.mp3"), Path("b.mp3")]
    assert resolve_clips(clips, {}, ["alert"]) == clips


def test_count_tagged():
    clips = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
    tags = {"a.mp3": ["alert"], "b.mp3": []}
    assert count_tagged(clips, tags) == 1
