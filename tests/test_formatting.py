import pytest

from generator import format_mmss, format_srt_time


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "00:00"), (59.4, "00:59"), (59.6, "01:00"), (125, "02:05"), (3600, "01:00:00"), (3725, "01:02:05")],
)
def test_format_mmss(seconds, expected):
    assert format_mmss(seconds) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "00:00:00,000"), (1.5, "00:00:01,500"), (61.25, "00:01:01,250"), (3661.001, "01:01:01,001")],
)
def test_format_srt_time(seconds, expected):
    assert format_srt_time(seconds) == expected


def test_format_srt_time_rounding_carries_into_seconds():
    # 0.9996s must become 00:00:01,000, never an invalid ",1000" millisecond field
    assert format_srt_time(0.9996) == "00:00:01,000"
    assert format_srt_time(59.9996) == "00:01:00,000"
