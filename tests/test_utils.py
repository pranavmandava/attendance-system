from datetime import datetime

import pytest
import pytz

from src.utils import ist_timestamp, string_to_timestamp


def test_string_to_timestamp_returns_none_for_none():
    assert string_to_timestamp(None) is None


def test_string_to_timestamp_returns_none_for_empty_string():
    assert string_to_timestamp("") is None


def test_string_to_timestamp_parses_iso_string_with_z_suffix():
    result = string_to_timestamp("2024-06-15T10:30:00Z")

    assert result == "2024-06-15T16:00:00+05:30"


def test_string_to_timestamp_parses_space_separated_datetime():
    result = string_to_timestamp("2024-06-15 10:30:00+00:00")

    assert result == "2024-06-15T16:00:00+05:30"


def test_string_to_timestamp_raises_value_error_for_malformed_input():
    with pytest.raises(ValueError):
        string_to_timestamp("not-a-valid-timestamp")


def test_ist_timestamp_returns_iso_format_with_ist_timezone():
    result = ist_timestamp()

    assert isinstance(result, str)
    assert "+05:30" in result
    assert "T" in result


def test_ist_timestamp_truncates_microseconds_to_milliseconds():
    result = ist_timestamp()
    dt = datetime.fromisoformat(result)
    ist = pytz.timezone("Asia/Kolkata")

    assert dt.utcoffset() == ist.utcoffset(dt.replace(tzinfo=None))
    assert dt.microsecond % 1000 == 0