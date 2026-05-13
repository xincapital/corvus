"""
INGEST-05: Members-only video skip tests.

Verifies that caption_downloader.py correctly:
1. Detects members-only error messages (pure function)
2. Raises MembersOnlyError (not a generic Exception) when Supadata signals
   members-only content, so the calling pipeline can skip the video cleanly.

Uses unittest.mock.patch — no pytest-mock required.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from caption_downloader import (
    MembersOnlyError,
    is_members_only_error,
)


class TestIsMembersOnlyError:
    """Pure function — no mocking needed."""

    @pytest.mark.parametrize("message", [
        "members-only",
        "Members-Only",
        "MEMBERS-ONLY",
        "members only",
        "join this channel",
        "available to members",
        "membership required",
        "channel membership",
        "private video",
        "this video is private",
        "Error: content available to members only",
        "SupadataError: join this channel to watch",
    ])
    def test_returns_true_for_members_only_indicators(self, message):
        assert is_members_only_error(message) is True

    @pytest.mark.parametrize("message", [
        "video not found",
        "transcript unavailable",
        "network timeout",
        "rate limit exceeded",
        "invalid video id",
        "",
        "400 Bad Request",
    ])
    def test_returns_false_for_non_members_errors(self, message):
        assert is_members_only_error(message) is False

    def test_accepts_non_string_input(self):
        """Callers pass exception objects; __str__ coercion must work."""
        exc = ValueError("members-only content")
        assert is_members_only_error(exc) is True


def _make_supadata_error(message: str):
    """Create a SupadataError-like exception with the given message."""
    try:
        from supadata import SupadataError
        return SupadataError(message)
    except (ImportError, Exception):
        err = Exception(message)
        return err


class TestDownloadCaptionsWithSupadataMembersOnly:
    """
    Verifies the Supadata download path raises MembersOnlyError (not a generic
    Exception) when SupadataError carries a members-only message.

    Uses unittest.mock.patch — no network calls.
    """

    def test_raises_members_only_error_on_supadata_members_only(self, tmp_path):
        """
        When Supadata raises SupadataError with 'members-only' text,
        download_captions_with_supadata must raise MembersOnlyError.
        """
        mock_client = MagicMock()
        mock_client.transcript.side_effect = _make_supadata_error(
            "Transcript unavailable: members-only content"
        )

        with (
            patch("caption_downloader.config.SUPADATA_KEYS", ["fake-key"]),
            patch("caption_downloader.Supadata", return_value=mock_client),
            patch("caption_downloader.SUPADATA_AVAILABLE", True),
        ):
            from caption_downloader import download_captions_with_supadata
            with pytest.raises(MembersOnlyError):
                download_captions_with_supadata(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    tmp_path,
                )

    def test_does_not_raise_members_only_error_for_generic_supadata_failure(self, tmp_path):
        """
        When Supadata raises SupadataError with a non-members-only message,
        download_captions_with_supadata must NOT raise MembersOnlyError.
        """
        mock_client = MagicMock()
        mock_client.transcript.side_effect = _make_supadata_error("Rate limit exceeded")

        with (
            patch("caption_downloader.config.SUPADATA_KEYS", ["fake-key"]),
            patch("caption_downloader.Supadata", return_value=mock_client),
            patch("caption_downloader.SUPADATA_AVAILABLE", True),
        ):
            from caption_downloader import download_captions_with_supadata
            with pytest.raises(Exception) as exc_info:
                download_captions_with_supadata(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    tmp_path,
                )
            assert not isinstance(exc_info.value, MembersOnlyError)


class TestDownloadCaptionsPropagatesMembersOnly:
    """
    Verifies the top-level download_captions() propagates MembersOnlyError
    without swallowing it (pipeline must be able to catch and skip the video).
    """

    def test_propagates_members_only_from_supadata(self, tmp_path):
        mock_client = MagicMock()
        mock_client.transcript.side_effect = MembersOnlyError("members-only")

        with (
            patch("caption_downloader.PLAYWRIGHT_AVAILABLE", False),
            patch("caption_downloader.SUPADATA_AVAILABLE", True),
            patch("caption_downloader.config.SUPADATA_KEYS", ["fake-key"]),
            patch("caption_downloader.Supadata", return_value=mock_client),
        ):
            from caption_downloader import download_captions
            with pytest.raises(MembersOnlyError):
                download_captions(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    tmp_path,
                )

    def test_propagates_members_only_from_notegpt(self, tmp_path):
        with (
            patch("caption_downloader.PLAYWRIGHT_AVAILABLE", True),
            patch(
                "caption_downloader._download_with_notegpt_retries",
                side_effect=MembersOnlyError("join this channel"),
            ),
        ):
            from caption_downloader import download_captions
            with pytest.raises(MembersOnlyError):
                download_captions(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    tmp_path,
                )
