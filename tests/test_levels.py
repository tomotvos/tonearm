import array

from tonearm.levels import SILENCE_DB, rms_dbfs


def pcm(value, samples=4410):
    return array.array("h", [value] * samples).tobytes()


def test_digital_silence_is_floor():
    assert rms_dbfs(pcm(0)) == SILENCE_DB


def test_empty_chunk_is_floor():
    assert rms_dbfs(b"") == SILENCE_DB


def test_full_scale_is_about_zero():
    assert abs(rms_dbfs(pcm(32767))) < 0.01


def test_half_scale_is_minus_six():
    assert abs(rms_dbfs(pcm(16384)) - (-6.02)) < 0.01


def test_odd_trailing_byte_is_ignored():
    assert rms_dbfs(pcm(16384) + b"\x01") == rms_dbfs(pcm(16384))
