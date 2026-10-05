"""Unit tests for LUMI Audio Synthesizer.

Tests voice selection, S3 key construction, error handling,
and the full synthesis + upload flow using mocked Polly and S3.
"""

import io
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

from audio_synthesizer import (
    VOICE_MAP,
    _build_s3_key,
    _estimate_duration,
    _get_voice_for_language,
    synthesize_audio,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_narrative() -> str:
    """Sample narrative text for audio synthesis testing."""
    return (
        "Good morning, Jennifer. Today is Monday, August 3, 2026. "
        "Your property is at 87 percent occupancy with ADR of 248 dollars. "
        "You have 7 VIP arrivals today including 3 Ambassador members."
    )


# ---------------------------------------------------------------------------
# Tests: Voice Selection
# ---------------------------------------------------------------------------


class TestVoiceSelection:
    """Tests for language-to-voice mapping."""

    def test_english_voice(self) -> None:
        """English maps to Joanna neural voice."""
        voice_id, engine = _get_voice_for_language("en-US")
        assert voice_id == "Joanna"
        assert engine == "neural"

    def test_spanish_voice(self) -> None:
        """Spanish maps to Lucia neural voice."""
        voice_id, engine = _get_voice_for_language("es-ES")
        assert voice_id == "Lucia"
        assert engine == "neural"

    def test_japanese_voice(self) -> None:
        """Japanese maps to Mizuki standard voice (Neural not available)."""
        voice_id, engine = _get_voice_for_language("ja-JP")
        assert voice_id == "Mizuki"
        assert engine == "standard"

    def test_chinese_voice(self) -> None:
        """Chinese Mandarin maps to Zhiyu neural voice."""
        voice_id, engine = _get_voice_for_language("zh-CN")
        assert voice_id == "Zhiyu"
        assert engine == "neural"

    def test_default_voice_for_unknown_language(self) -> None:
        """Unknown language falls back to English default (Joanna neural)."""
        voice_id, engine = _get_voice_for_language("fr-FR")
        assert voice_id == "Joanna"
        assert engine == "neural"

    def test_voice_map_has_four_languages(self) -> None:
        """VOICE_MAP contains exactly 4 supported languages."""
        assert len(VOICE_MAP) == 4
        assert set(VOICE_MAP.keys()) == {"en-US", "es-ES", "ja-JP", "zh-CN"}


# ---------------------------------------------------------------------------
# Tests: S3 Key Construction
# ---------------------------------------------------------------------------


class TestS3KeyConstruction:
    """Tests for S3 path pattern generation."""

    def test_standard_date_format(self) -> None:
        """S3 key follows briefs/{year}/{month}/{day}/{propertyId}/morning-brief.mp3."""
        key = _build_s3_key("ALOHA-CHI-001", "2026-08-03")
        assert key == "briefs/2026/08/03/ALOHA-CHI-001/morning-brief.mp3"

    def test_different_property_and_date(self) -> None:
        """S3 key uses provided property ID and date components."""
        key = _build_s3_key("ALOHA-TKY-002", "2026-12-25")
        assert key == "briefs/2026/12/25/ALOHA-TKY-002/morning-brief.mp3"

    def test_single_digit_month_preserves_padding(self) -> None:
        """Zero-padded single-digit months are preserved in the key."""
        key = _build_s3_key("ALOHA-LON-003", "2026-01-05")
        assert key == "briefs/2026/01/05/ALOHA-LON-003/morning-brief.mp3"


# ---------------------------------------------------------------------------
# Tests: Error Handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for graceful error handling returning TEXT_ONLY status."""

    @patch("audio_synthesizer._polly_client")
    def test_polly_error_returns_text_only(
        self, mock_polly: MagicMock, sample_narrative: str
    ) -> None:
        """Polly API failure results in TEXT_ONLY status (no exception raised)."""
        # Simulate a Polly ClientError
        mock_polly.synthesize_speech.side_effect = ClientError(
            error_response={"Error": {"Code": "ServiceFailure", "Message": "Service unavailable"}},
            operation_name="SynthesizeSpeech",
        )
        # Mock the exceptions attribute for specific exception handling
        mock_polly.exceptions = MagicMock()
        mock_polly.exceptions.TextLengthExceededException = type(
            "TextLengthExceededException", (ClientError,), {}
        )
        mock_polly.exceptions.InvalidSsmlException = type(
            "InvalidSsmlException", (ClientError,), {}
        )

        result = synthesize_audio(sample_narrative, "en-US", "ALOHA-CHI-001", "2026-08-03")

        assert result["status"] == "TEXT_ONLY"
        assert result["s3Key"] is None
        assert result["cloudFrontUrl"] is None
        assert result["voiceId"] == "Joanna"

    @patch("audio_synthesizer._s3_client")
    @patch("audio_synthesizer._polly_client")
    def test_s3_error_returns_text_only(
        self, mock_polly: MagicMock, mock_s3: MagicMock, sample_narrative: str
    ) -> None:
        """S3 upload failure results in TEXT_ONLY status."""
        # Polly succeeds
        mock_audio_stream = MagicMock()
        mock_audio_stream.read.return_value = b"fake-mp3-data"
        mock_polly.synthesize_speech.return_value = {"AudioStream": mock_audio_stream}
        mock_polly.exceptions = MagicMock()
        mock_polly.exceptions.TextLengthExceededException = type(
            "TextLengthExceededException", (ClientError,), {}
        )
        mock_polly.exceptions.InvalidSsmlException = type(
            "InvalidSsmlException", (ClientError,), {}
        )

        # S3 fails
        mock_s3.put_object.side_effect = ClientError(
            error_response={"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            operation_name="PutObject",
        )
        mock_s3.exceptions = MagicMock()
        mock_s3.exceptions.NoSuchBucket = type("NoSuchBucket", (ClientError,), {})

        result = synthesize_audio(sample_narrative, "en-US", "ALOHA-CHI-001", "2026-08-03")

        assert result["status"] == "TEXT_ONLY"
        assert result["s3Key"] is None

    @patch("audio_synthesizer._s3_client")
    @patch("audio_synthesizer._polly_client")
    def test_successful_synthesis_returns_ready(
        self, mock_polly: MagicMock, mock_s3: MagicMock, sample_narrative: str
    ) -> None:
        """Successful Polly + S3 flow returns READY status with metadata."""
        # Polly succeeds
        mock_audio_stream = MagicMock()
        mock_audio_stream.read.return_value = b"fake-mp3-data-content"
        mock_polly.synthesize_speech.return_value = {"AudioStream": mock_audio_stream}
        mock_polly.exceptions = MagicMock()
        mock_polly.exceptions.TextLengthExceededException = type(
            "TextLengthExceededException", (ClientError,), {}
        )
        mock_polly.exceptions.InvalidSsmlException = type(
            "InvalidSsmlException", (ClientError,), {}
        )

        # S3 succeeds
        mock_s3.put_object.return_value = {}
        mock_s3.exceptions = MagicMock()
        mock_s3.exceptions.NoSuchBucket = type("NoSuchBucket", (ClientError,), {})

        result = synthesize_audio(sample_narrative, "en-US", "ALOHA-CHI-001", "2026-08-03")

        assert result["status"] == "READY"
        assert result["s3Key"] == "briefs/2026/08/03/ALOHA-CHI-001/morning-brief.mp3"
        assert result["cloudFrontUrl"] is not None
        assert "d1234567890.cloudfront.net" in result["cloudFrontUrl"]
        assert result["voiceId"] == "Joanna"
        assert result["engine"] == "neural"
        assert result["durationSeconds"] > 0
        assert result["briefId"] is not None


# ---------------------------------------------------------------------------
# Tests: Duration Estimation
# ---------------------------------------------------------------------------


class TestDurationEstimation:
    """Tests for the audio duration estimation helper."""

    def test_short_text_duration(self) -> None:
        """Short text (30 words) estimates to roughly 12-13 seconds."""
        text = " ".join(["word"] * 30)
        duration = _estimate_duration(text)
        # 30 words / 150 wpm = 0.2 min = 12 seconds + 1
        assert duration == 13

    def test_target_length_duration(self) -> None:
        """150-word narrative estimates to ~61 seconds (target 60-90s)."""
        text = " ".join(["word"] * 150)
        duration = _estimate_duration(text)
        # 150 / 150 = 1.0 min = 60 seconds + 1
        assert duration == 61
