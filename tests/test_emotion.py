import pytest

from generator import (
    EMOTION_ALIAS_MAP,
    VOCAL_EMOTION_SPECS,
    _is_male_voice_id,
    extract_speaker_and_emotion_prefix,
    extract_speaker_prefix,
    get_emotion_pause_sec,
    parse_emotion_and_intensity,
)


def test_empty_emotion_defaults_to_medium():
    assert parse_emotion_and_intensity("") == ("", 0.78)
    assert parse_emotion_and_intensity(None) == ("", 0.78)


@pytest.mark.parametrize(
    "suffix,scale",
    [("light", 0.50), ("nhẹ", 0.50), ("30%", 0.50), ("medium", 0.78), ("strong", 1.00), ("mạnh", 1.00), ("100%", 1.00)],
)
def test_intensity_suffixes(suffix, scale):
    alias = next(iter(EMOTION_ALIAS_MAP))
    canon, got = parse_emotion_and_intensity(f"{alias}:{suffix}")
    assert canon == EMOTION_ALIAS_MAP[alias]
    assert got == scale


def test_numeric_intensity_is_clamped():
    alias = next(iter(EMOTION_ALIAS_MAP))
    assert parse_emotion_and_intensity(f"{alias}:0")[1] == 0.35 or parse_emotion_and_intensity(f"{alias}:0")[1] == 0.50
    assert 0.35 <= parse_emotion_and_intensity(f"{alias}:45")[1] <= 1.0
    assert parse_emotion_and_intensity(f"{alias}:9999")[1] == 1.0


def test_garbage_intensity_keeps_default():
    alias = next(iter(EMOTION_ALIAS_MAP))
    assert parse_emotion_and_intensity(f"{alias}:banana")[1] == 0.78


def test_unknown_emotion_returns_empty_key():
    assert parse_emotion_and_intensity("zzz-not-an-emotion")[0] == ""


def test_blended_emotion_resolves_to_known_keys():
    keys = [k for k in VOCAL_EMOTION_SPECS if "+" not in k][:2]
    canon, _ = parse_emotion_and_intensity(f"{keys[0]}+{keys[1]}")
    assert set(canon.split("+")) == set(keys)


def test_pause_is_between_baseline_and_spec():
    for key in list(VOCAL_EMOTION_SPECS)[:6]:
        pause = get_emotion_pause_sec(key)
        base = float(VOCAL_EMOTION_SPECS[key].get("pause_sec", 0.42))
        assert min(0.40, base) - 0.01 <= pause <= max(0.40, base) + 0.01


def test_speaker_prefix_plain():
    assert extract_speaker_prefix("Anna: Hello there") == ("Anna", "Hello there")
    assert extract_speaker_prefix("Tom: Hi") == ("Tom", "Hi")
    assert extract_speaker_prefix("Jack: Yo") == ("Jack", "Yo")
    assert extract_speaker_prefix("Mia: Hey") == ("Mia", "Hey")


def test_speaker_aliases_map_to_canonical_names():
    assert extract_speaker_prefix("Nam: Hi")[0] == "Tom"
    assert extract_speaker_prefix("Speaker B: Hi")[0] == "Tom"
    assert extract_speaker_prefix("Nữ: Hi")[0] == "Anna"


def test_no_prefix_uses_default_speaker():
    assert extract_speaker_and_emotion_prefix("Just text", "Mia") == ("Mia", "", "Just text")


def test_emotion_tag_with_speaker_and_monologue():
    sp, emo, text = extract_speaker_and_emotion_prefix("Anna [sad]: I miss you")
    assert (sp, text) == ("Anna", "I miss you")
    assert emo != ""
    sp, emo2, text = extract_speaker_and_emotion_prefix("[sad] I miss you", "Anna")
    assert (sp, text) == ("Anna", "I miss you")
    assert emo2 == emo


def test_male_voice_detection():
    assert _is_male_voice_id("gemini:Puck")
    assert _is_male_voice_id("en-US-GuyNeural")
    assert not _is_male_voice_id("gemini:Aoede")
    assert not _is_male_voice_id("")
