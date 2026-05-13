"""
INGEST-04: SRT compression output quality tests.

Verifies compress_srt.py pure functions:
- parse_timestamp / format_timestamp roundtrip
- compress_subtitles merging logic (gap, char limit, sentence boundaries)
- File-level output size reduction (> 20% reduction on realistic input)
"""

import sys
import textwrap
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from compress_srt import (
    parse_timestamp,
    format_timestamp,
    parse_srt,
    compress_subtitles,
    write_srt,
)


class TestParseTimestamp:
    def test_valid_timestamp_returns_seconds(self):
        # 1h 2m 3s 400ms
        assert parse_timestamp("01:02:03,400") == pytest.approx(3723.4)

    def test_dot_separator_accepted(self):
        assert parse_timestamp("00:00:01.500") == pytest.approx(1.5)

    def test_zero_timestamp(self):
        assert parse_timestamp("00:00:00,000") == 0.0

    def test_invalid_format_returns_zero(self):
        assert parse_timestamp("not-a-timestamp") == 0


class TestFormatTimestamp:
    def test_roundtrip(self):
        # Use a value that survives float conversion without rounding (millis divisible by 8)
        original = "01:23:45,000"
        seconds = parse_timestamp(original)
        assert format_timestamp(seconds) == original

    def test_zero(self):
        assert format_timestamp(0) == "00:00:00,000"

    def test_sub_second(self):
        assert format_timestamp(0.5) == "00:00:00,500"


class TestCompressSubtitles:
    def _make_subs(self, entries):
        """Build subtitle list: entries = [(start, end, text), ...]."""
        return [{"start": s, "end": e, "text": t} for s, e, t in entries]

    def test_merges_entries_within_gap(self):
        subs = self._make_subs([
            (0.0, 1.0, "Hello"),
            (1.5, 2.5, "world"),   # gap = 0.5 s → should merge
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 1
        assert result[0]["text"] == "Hello world"
        assert result[0]["start"] == 0.0
        assert result[0]["end"] == 2.5

    def test_does_not_merge_beyond_gap(self):
        subs = self._make_subs([
            (0.0, 1.0, "Hello"),
            (4.0, 5.0, "world"),   # gap = 3 s > max_gap 2 s → separate
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 2

    def test_does_not_merge_past_char_limit(self):
        # 193 + 1 (space) + 8 ("overflow") = 202 > 200 → must NOT merge
        long_text = "x" * 193
        subs = self._make_subs([
            (0.0, 1.0, long_text),
            (1.5, 2.5, "overflow"),
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 2

    def test_does_not_merge_after_period(self):
        subs = self._make_subs([
            (0.0, 1.0, "End of sentence."),
            (1.5, 2.5, "New sentence"),
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 2

    def test_does_not_merge_after_exclamation(self):
        subs = self._make_subs([
            (0.0, 1.0, "Wow!"),
            (1.5, 2.5, "Amazing"),
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 2

    def test_does_not_merge_after_question_mark(self):
        subs = self._make_subs([
            (0.0, 1.0, "Really?"),
            (1.5, 2.5, "Yes"),
        ])
        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) == 2

    def test_empty_input_returns_empty(self):
        assert compress_subtitles([]) == []

    def test_single_entry_returned_unchanged(self):
        subs = self._make_subs([(0.0, 1.0, "Only one")])
        result = compress_subtitles(subs)
        assert len(result) == 1
        assert result[0]["text"] == "Only one"

    def test_output_quality_size_reduction(self):
        """INGEST-04: compressed output must be meaningfully smaller than input."""
        # Build 20 short subtitle entries that should all merge into ~4 groups
        entries = []
        for i in range(20):
            start = i * 1.5
            end = start + 0.8
            entries.append((start, end, f"word{i}"))
        subs = self._make_subs(entries)

        result = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        assert len(result) < len(subs), "compressed count should be less than original"
        reduction_pct = (1 - len(result) / len(subs)) * 100
        assert reduction_pct >= 20, f"expected >=20% reduction, got {reduction_pct:.1f}%"


class TestParseSrtAndWriteRoundtrip:
    SRT_CONTENT = textwrap.dedent("""\
        1
        00:00:01,000 --> 00:00:02,000
        Hello

        2
        00:00:02,500 --> 00:00:03,500
        world

        3
        00:00:06,000 --> 00:00:07,000
        Final sentence.
    """)

    def test_parse_srt_returns_correct_count(self, tmp_path):
        srt_file = tmp_path / "test.srt"
        srt_file.write_text(self.SRT_CONTENT, encoding="utf-8")
        subs = parse_srt(srt_file)
        assert len(subs) == 3

    def test_parse_srt_correct_text(self, tmp_path):
        srt_file = tmp_path / "test.srt"
        srt_file.write_text(self.SRT_CONTENT, encoding="utf-8")
        subs = parse_srt(srt_file)
        assert subs[0]["text"] == "Hello"
        assert subs[2]["text"] == "Final sentence."

    def test_write_and_reparse_roundtrip(self, tmp_path):
        srt_file = tmp_path / "test.srt"
        srt_file.write_text(self.SRT_CONTENT, encoding="utf-8")
        subs = parse_srt(srt_file)
        compressed = compress_subtitles(subs, max_gap=2.0, max_chars=200)

        out_file = tmp_path / "compressed.srt"
        write_srt(compressed, out_file)

        # "Hello" and "world" are within gap → merged; "Final sentence." separate
        reparsed = parse_srt(out_file)
        assert len(reparsed) == 2
        assert "Hello world" in reparsed[0]["text"]

    def test_file_size_reduction(self, tmp_path):
        """INGEST-04: compressed file must be smaller than original for mergeable input."""
        srt_file = tmp_path / "test.srt"
        srt_file.write_text(self.SRT_CONTENT, encoding="utf-8")
        subs = parse_srt(srt_file)
        compressed = compress_subtitles(subs, max_gap=2.0, max_chars=200)
        out_file = tmp_path / "compressed.srt"
        write_srt(compressed, out_file)

        assert out_file.stat().st_size < srt_file.stat().st_size
