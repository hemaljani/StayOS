"""LUMI Audio Synthesizer - text-to-speech via Amazon Polly with S3 storage.

Converts the AI-generated narrative into an MP3 audio file using Amazon Polly
with language-specific voice selection, then uploads the result to S3 for
CloudFront streaming delivery. Returns audio metadata for the brief record.

Satisfies REQ-15 (Polly TTS) and REQ-26 (Audio Language Configuration).
"""

import os
import uuid
from typing import Any, Dict, Tuple

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

from orchestrator_exceptions import AudioSynthesisError

logger = Logger(service="stayos-orchestrator")

# Module-level configuration from environment variables
AUDIO_BUCKET_NAME = os.environ.get("AUDIO_BUCKET_NAME", "")
AUDIO_CLOUDFRONT_DOMAIN = os.environ.get("AUDIO_CLOUDFRONT_DOMAIN", "")

# Language-to-voice mapping: (VoiceId, Engine)
# Neural engine provides higher quality; Standard used where Neural unavailable
VOICE_MAP: Dict[str, Tuple[str, str]] = {
    "en-US": ("Joanna", "neural"),
    "es-ES": ("Lucia", "neural"),
    "ja-JP": ("Mizuki", "standard"),
    "zh-CN": ("Zhiyu", "neural"),
}

# Default voice when language preference is not in VOICE_MAP
DEFAULT_LANGUAGE = "en-US"

# Polly synthesis parameters
OUTPUT_FORMAT = "mp3"
SAMPLE_RATE = "24000"

# Estimated speaking rate: ~150 words per minute for English
# Used to estimate audio duration from word count
WORDS_PER_MINUTE = 150

# Module-level boto3 clients (connection reuse across Lambda invocations)
_polly_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=10,
    read_timeout=30,
)
_polly_client = boto3.client("polly", config=_polly_config)

_s3_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
)
_s3_client = boto3.client("s3", config=_s3_config)


def synthesize_audio(
    narrative: str, language: str, property_id: str, date_str: str
) -> Dict[str, Any]:
    """Synthesize speech from narrative text and upload to S3.

    Selects the appropriate Polly voice based on language preference,
    synthesizes the narrative to MP3, uploads to S3 at the standard
    path, and returns audio metadata for the brief record.

    On failure (Polly or S3 errors), returns a TEXT_ONLY status rather
    than raising - the brief can still be delivered without audio.

    Args:
        narrative: The validated narrative text to synthesize.
        language: Language code (e.g., "en-US") for voice selection.
        property_id: Property identifier for the S3 path (e.g., "ALOHA-CHI-001").
        date_str: Date string in YYYY-MM-DD format for the S3 path.

    Returns:
        Audio metadata dict with keys:
        - briefId: Unique identifier for this audio brief.
        - durationSeconds: Estimated audio duration.
        - s3Key: S3 object key where the MP3 is stored (or None on failure).
        - cloudFrontUrl: Full streaming URL (or None on failure).
        - status: "READY" on success, "TEXT_ONLY" on failure.
        - voiceId: The Polly voice used.
        - engine: The Polly engine used ("neural" or "standard").
    """
    brief_id = str(uuid.uuid4())
    voice_id, engine = _get_voice_for_language(language)
    s3_key = _build_s3_key(property_id, date_str)

    logger.info(
        "Synthesizing audio brief",
        brief_id=brief_id,
        language=language,
        voice_id=voice_id,
        engine=engine,
        property_id=property_id,
    )

    try:
        # Call Amazon Polly to synthesize speech
        audio_stream = _call_polly(narrative, voice_id, engine)

        # Upload the MP3 audio to S3
        _upload_to_s3(audio_stream, s3_key)

        # Estimate duration based on word count
        duration_seconds = _estimate_duration(narrative)

        # Build the CloudFront streaming URL (read env at runtime for testability)
        audio_cloudfront_domain = os.environ.get("AUDIO_CLOUDFRONT_DOMAIN", "")
        cloudfront_url = f"https://{audio_cloudfront_domain}/{s3_key}"

        logger.info(
            "Audio synthesis and upload complete",
            brief_id=brief_id,
            s3_key=s3_key,
            duration_seconds=duration_seconds,
        )

        return {
            "briefId": brief_id,
            "durationSeconds": duration_seconds,
            "s3Key": s3_key,
            "cloudFrontUrl": cloudfront_url,
            "status": "READY",
            "voiceId": voice_id,
            "engine": engine,
        }

    except AudioSynthesisError as error:
        # Already logged at the point of failure; return TEXT_ONLY status
        logger.error(
            "Audio synthesis failed - brief will be text-only",
            brief_id=brief_id,
            error=str(error),
            property_id=property_id,
        )
        return {
            "briefId": brief_id,
            "durationSeconds": 0,
            "s3Key": None,
            "cloudFrontUrl": None,
            "status": "TEXT_ONLY",
            "voiceId": voice_id,
            "engine": engine,
        }


def _get_voice_for_language(language: str) -> Tuple[str, str]:
    """Select the Polly voice and engine for the given language.

    Falls back to the default voice (Joanna, neural) if the language
    is not in the VOICE_MAP.

    Args:
        language: Language code (e.g., "en-US", "ja-JP").

    Returns:
        Tuple of (voice_id, engine) for the Polly API call.
    """
    if language not in VOICE_MAP:
        logger.warning(
            "Unknown language - using default voice",
            requested_language=language,
            default_language=DEFAULT_LANGUAGE,
        )
        return VOICE_MAP[DEFAULT_LANGUAGE]

    return VOICE_MAP[language]


def _build_s3_key(property_id: str, date_str: str) -> str:
    """Construct the S3 object key for the audio file.

    Path pattern: briefs/{year}/{month}/{day}/{propertyId}/morning-brief.mp3

    Args:
        property_id: Property identifier (e.g., "ALOHA-CHI-001").
        date_str: Date in YYYY-MM-DD format (e.g., "2026-08-03").

    Returns:
        S3 object key string.
    """
    # Parse date components from YYYY-MM-DD format
    parts = date_str.split("-")
    year = parts[0]
    month = parts[1]
    day = parts[2]

    return f"briefs/{year}/{month}/{day}/{property_id}/morning-brief.mp3"


def _call_polly(narrative: str, voice_id: str, engine: str) -> bytes:
    """Call Amazon Polly to synthesize speech from text.

    Uses the synthesize_speech API with MP3 output at 24kHz sample rate
    for high-quality mobile playback.

    Args:
        narrative: Text to synthesize.
        voice_id: Polly voice identifier (e.g., "Joanna").
        engine: Polly engine type ("neural" or "standard").

    Returns:
        Raw MP3 audio bytes.

    Raises:
        AudioSynthesisError: If Polly returns an error.
    """
    try:
        # Synthesize speech with high-quality settings for mobile playback
        response = _polly_client.synthesize_speech(
            OutputFormat=OUTPUT_FORMAT,
            SampleRate=SAMPLE_RATE,
            Text=narrative,
            VoiceId=voice_id,
            Engine=engine,
        )

        # Read the audio stream into bytes
        audio_stream = response["AudioStream"].read()

        logger.info(
            "Polly synthesis successful",
            voice_id=voice_id,
            engine=engine,
            audio_bytes=len(audio_stream),
        )

        return audio_stream

    except _polly_client.exceptions.TextLengthExceededException as error:
        logger.error(
            "Narrative exceeds Polly text length limit",
            error=str(error),
            text_length=len(narrative),
        )
        raise AudioSynthesisError(f"Text too long for Polly: {len(narrative)} chars")

    except _polly_client.exceptions.InvalidSsmlException as error:
        logger.error("Invalid SSML in narrative", error=str(error))
        raise AudioSynthesisError(f"Invalid SSML: {error}")

    except ClientError as error:
        # Catch-all for unexpected Polly errors at top-level
        logger.error(
            "Unexpected Polly error",
            error=str(error),
            voice_id=voice_id,
        )
        raise AudioSynthesisError(f"Polly API error: {error}")


def _upload_to_s3(audio_bytes: bytes, s3_key: str) -> None:
    """Upload MP3 audio to the configured S3 bucket.

    Sets content type and cache control headers for optimal
    CloudFront streaming performance.

    Args:
        audio_bytes: Raw MP3 audio data.
        s3_key: S3 object key (path within the bucket).

    Raises:
        AudioSynthesisError: If the S3 upload fails.
    """
    try:
        # Read bucket name at runtime (allows monkeypatch in tests)
        bucket_name = os.environ.get("AUDIO_BUCKET_NAME", AUDIO_BUCKET_NAME)

        # Upload with content type for streaming and cache headers
        _s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=audio_bytes,
            ContentType="audio/mpeg",
            CacheControl="max-age=86400",
        )

        logger.info(
            "Audio uploaded to S3",
            bucket=bucket_name,
            key=s3_key,
            size_bytes=len(audio_bytes),
        )

    except _s3_client.exceptions.NoSuchBucket as error:
        logger.error(
            "Audio bucket does not exist",
            bucket=bucket_name,
            error=str(error),
        )
        raise AudioSynthesisError(f"S3 bucket not found: {bucket_name}")

    except ClientError as error:
        # Catch-all for unexpected S3 errors at top-level
        logger.error(
            "S3 upload failed",
            bucket=bucket_name,
            key=s3_key,
            error=str(error),
        )
        raise AudioSynthesisError(f"S3 upload error: {error}")


def _estimate_duration(narrative: str) -> int:
    """Estimate audio duration in seconds based on word count.

    Uses an approximate speaking rate of 150 words per minute.
    This is a rough estimate - actual duration depends on the voice,
    language, and synthesis engine.

    Args:
        narrative: The narrative text.

    Returns:
        Estimated duration in seconds (rounded up).
    """
    word_count = len(narrative.split())
    duration_minutes = word_count / WORDS_PER_MINUTE
    duration_seconds = int(duration_minutes * 60) + 1
    return duration_seconds
