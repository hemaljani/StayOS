"""Validate CloudFront signatures and property boundaries without AWS writes."""

import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

import audio_signing
from exceptions import ForbiddenError
from handlers import briefs_handler

DOMAIN = "d123.cloudfront.net"
KEY = "briefs/2026/11/03/ALOHA-TYO-001/morning-brief.mp3"


@pytest.fixture(autouse=True)
def clear_signer_cache():
    audio_signing._signer.cache_clear()
    yield
    audio_signing._signer.cache_clear()


def test_signature_matches_public_key_and_expires_in_15_minutes(audio_test_private_key):
    before = int(datetime.now(timezone.utc).timestamp())
    signed, expiry = audio_signing.sign_brief_audio(DOMAIN, KEY, "ALOHA-TYO-001")
    url = urlsplit(signed)
    query = parse_qs(url.query)
    expires = int(query["Expires"][0])
    assert before + 899 <= expires <= before + 901
    assert int(datetime.fromisoformat(expiry).timestamp()) == expires
    assert query["Key-Pair-Id"] == ["TESTKEY"]
    signature = base64.b64decode(
        query["Signature"][0].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    policy = json.dumps({
        "Statement": [{
            "Resource": f"https://{DOMAIN}/{KEY}",
            "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
        }],
    }, separators=(",", ":")).encode()
    audio_test_private_key.public_key().verify(signature, policy, padding.PKCS1v15(), hashes.SHA1())


@pytest.mark.parametrize("key", [
    KEY.replace("ALOHA-TYO-001", "ALOHA-CHI-001"),
    KEY + "?x=1", KEY + "/../morning-brief.mp3",
    "https://other.example/" + KEY, "../" + KEY,
])
def test_cannot_sign_foreign_or_ambiguous_keys(key):
    with pytest.raises(RuntimeError, match="authorized property"):
        audio_signing.sign_brief_audio(DOMAIN, key, "ALOHA-TYO-001")


def test_missing_private_key_fails_closed(monkeypatch):
    monkeypatch.delenv("AUDIO_SIGNING_PRIVATE_KEY")
    with pytest.raises(RuntimeError, match="not configured"):
        audio_signing.sign_brief_audio(DOMAIN, KEY, "ALOHA-TYO-001")


def test_foreign_property_request_does_not_read_or_sign(monkeypatch):
    database, signer = MagicMock(), MagicMock()
    monkeypatch.setattr(briefs_handler, "_dynamodb_resource", database)
    monkeypatch.setattr(briefs_handler, "sign_brief_audio", signer)
    event = {"requestContext": {"authorizer": {"claims": {"custom:propertyId": "ALOHA-TYO-001"}}}}
    with pytest.raises(ForbiddenError):
        briefs_handler.get_brief(event, {"propertyId": "ALOHA-CHI-001"})
    database.Table.assert_not_called()
    signer.assert_not_called()


def test_stored_legacy_url_is_not_returned(monkeypatch):
    monkeypatch.setattr(briefs_handler, "sign_brief_audio", lambda *args: ("fresh-signed", "expiry"))
    brief = {"audioBrief": {"s3Key": KEY, "audioUrl": "legacy-public"}}
    briefs_handler._attach_audio_url(brief, "ALOHA-TYO-001")
    assert brief["audioBrief"]["audioUrl"] == "fresh-signed"
