"""Issue short-lived CloudFront capabilities for one authorized brief object.

The private key is resolved by CloudFormation into the Lambda environment.
This module never retrieves Secrets Manager values or logs signing material.
"""

import os
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from botocore.signers import CloudFrontSigner
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

URL_TTL_SECONDS = 900


@lru_cache(maxsize=1)
def _signer() -> CloudFrontSigner:
    """Parse the deployment-resolved RSA key once per execution environment."""
    key_id = os.environ.get("AUDIO_SIGNING_KEY_ID", "")
    pem = os.environ.get("AUDIO_SIGNING_PRIVATE_KEY", "")
    if not key_id or not pem:
        raise RuntimeError("Audio signing is not configured")
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise RuntimeError("Audio signing requires an RSA 2048 key")

    def sign(message: bytes) -> bytes:
        # CloudFront's RSA signed-URL protocol requires SHA-1 with PKCS#1 v1.5.
        return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    return CloudFrontSigner(key_id, sign)


def sign_brief_audio(domain: str, object_key: str, property_id: str) -> tuple[str, str]:
    """Sign only an audio object in the already authorized property's prefix."""
    if not re.fullmatch(r"[a-z0-9-]+\.cloudfront\.net", domain):
        raise RuntimeError("Invalid audio distribution domain")
    pattern = (
        r"briefs/\d{4}/\d{2}/\d{2}/"
        + re.escape(property_id)
        + r"/morning-brief(?:-[A-Za-z0-9_-]+)?\.mp3"
    )
    if not re.fullmatch(r"ALOHA-[A-Z]{3}-\d{3}", property_id) or not re.fullmatch(
        pattern, object_key
    ):
        raise RuntimeError("Audio object does not belong to the authorized property")
    expires = datetime.now(UTC) + timedelta(seconds=URL_TTL_SECONDS)
    url = _signer().generate_presigned_url(
        f"https://{domain}/{object_key}", date_less_than=expires
    )
    return url, expires.isoformat()
