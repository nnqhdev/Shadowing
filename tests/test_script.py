from generator import (
    build_fallback_script,
    parse_custom_script_text,
    split_thumbnail_title_lines,
    split_thumbnail_title_parts,
)


def test_custom_script_parses_speakers_and_emotions():
    script = parse_custom_script_text("Anna [sad]: I miss you\nTom: Me too", "topic", "B1", "cat")
    lines = [ln for scene in script["scenes"] for ln in scene["lines"]]
    assert [ln["speaker"] for ln in lines] == ["Anna", "Tom"]
    assert lines[0]["emotion"] == "sad"
    assert lines[0]["en"] == "I miss you"
    assert script["podcast_mode"] == "dialogue"


def test_explicit_level_header_wins_over_estimate():
    script = parse_custom_script_text("Level: C1\nAnna: Hello", "topic", "B1", "cat")
    assert script["level"] == "C1"


def test_fallback_script_has_required_shape():
    script = build_fallback_script("Drama bạn thân", "B1", "cat")
    assert {"title_vi", "level", "scenes", "thumb_prompt"} <= set(script)
    assert script["scenes"] and all(scene["lines"] for scene in script["scenes"])


def test_thumbnail_title_split_is_uppercase_and_bounded():
    lines, last = split_thumbnail_title_lines("Cô gái bị bạn thân phản bội ngay trước đám cưới")
    assert all(part == part.upper() for part in lines + [last])
    assert 1 <= len(lines) <= 3
    top, bottom = split_thumbnail_title_parts("Cô gái bị bạn thân phản bội")
    assert top and bottom
