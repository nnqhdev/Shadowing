import struct

from generator import trim_pcm_silence

SR = 24000


def _pcm(values):
    return struct.pack(f"<{len(values)}h", *values)


def test_short_input_is_returned_unchanged():
    data = _pcm([0] * 100)
    assert trim_pcm_silence(data) == data


def test_all_silence_is_returned_unchanged():
    data = _pcm([0] * SR)
    assert trim_pcm_silence(data) == data


def test_leading_and_trailing_silence_trimmed_but_speech_kept():
    speech = [5000, -5000] * 2000
    data = _pcm([0] * SR + speech + [0] * SR)
    out = trim_pcm_silence(data, SR)
    assert len(out) < len(data)
    # all speech samples survive intact
    assert _pcm(speech) in out
    # keeps a small lead/trail pad (70ms / 120ms) rather than cutting flush
    expected = (int(0.07 * SR) + len(speech) + int(0.12 * SR)) * 2
    assert abs(len(out) - expected) <= 4
