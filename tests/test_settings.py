import json

from tonearm.settings import Settings, load, save


def test_missing_file_gives_defaults(tmp_path):
    assert load(tmp_path / "s.json") == Settings()


def test_round_trip(tmp_path):
    path = tmp_path / "sub" / "s.json"
    original = Settings(input="plughw:1,0", default_speakers=["1", "2"], auto_on=False)
    save(original, path)
    assert load(path) == original


def test_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert load(path) == Settings()


def test_wrong_types_are_ignored(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"input": 5, "default_speakers": "x", "auto_on": "yes"}))
    assert load(path) == Settings()


def test_non_string_speaker_ids_are_dropped(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"default_speakers": ["1", 2, None, "3"]}))
    assert load(path).default_speakers == ["1", "3"]


def test_save_leaves_no_temp_file(tmp_path):
    path = tmp_path / "s.json"
    save(Settings(), path)
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]
