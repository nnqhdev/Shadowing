import pytest

from generator import sanitize_english_for_tts


def test_empty_input_falls_back_to_hello():
    assert sanitize_english_for_tts("") == "Hello."
    assert sanitize_english_for_tts(None) == "Hello."


def test_leading_ellipsis_removed():
    assert sanitize_english_for_tts("...I deserve that.") == "I deserve that."


def test_mid_sentence_ellipsis_becomes_comma_pause():
    assert sanitize_english_for_tts("I think... maybe not") == "I think, maybe not"


def test_written_interjections_replaced():
    out = sanitize_english_for_tts("Ugh, fine. Haha that was funny")
    assert "ugh" not in out.lower()
    assert "Oh man" in out
    assert "Ha ha" in out


def test_vietnamese_tokens_stripped():
    out = sanitize_english_for_tts("Hello xin chào friend")
    assert "chào" not in out
    assert out.startswith("Hello") and out.endswith("friend")


def test_quotes_dropped_and_starter_comma_removed():
    assert '"' not in sanitize_english_for_tts('He said "Take care" and left')
    assert sanitize_english_for_tts("No, I'm not going") == "No I'm not going"


def test_whitespace_collapsed():
    assert sanitize_english_for_tts("  a   b \n c ") == "a b c"
