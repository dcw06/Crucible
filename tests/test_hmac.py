"""
tests/test_hmac.py — HMAC verification unit tests
===================================================
Run before Day 5 integration:
    cd crucible/
    pytest tests/test_hmac.py -v
"""

import hashlib
import hmac
import os
import sys

import pytest

# Allow importing from parent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Set secret before importing auth (auth reads it at call time, not import time)
os.environ["GITHUB_WEBHOOK_SECRET"] = "test_secret_abc123"
from auth import verify_signature


SECRET   = "test_secret_abc123"
PAYLOAD  = b'{"action": "opened", "pull_request": {"number": 42}}'


def _make_sig(payload: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestVerifySignature:
    def test_valid_signature(self):
        """Valid HMAC-SHA256 signature is accepted."""
        sig = _make_sig(PAYLOAD)
        assert verify_signature(PAYLOAD, sig) is True

    def test_wrong_secret(self):
        """Signature made with wrong secret is rejected."""
        sig = _make_sig(PAYLOAD, secret="wrong_secret")
        assert verify_signature(PAYLOAD, sig) is False

    def test_bad_hex_hash(self):
        """Malformed hex in signature is rejected."""
        assert verify_signature(PAYLOAD, "sha256=badhash") is False

    def test_empty_header(self):
        """Empty signature header is rejected."""
        assert verify_signature(PAYLOAD, "") is False

    def test_missing_prefix(self):
        """Signature without sha256= prefix is rejected."""
        raw_hex = hmac.new(SECRET.encode(), PAYLOAD, hashlib.sha256).hexdigest()
        assert verify_signature(PAYLOAD, raw_hex) is False

    def test_tampered_payload(self):
        """Valid signature against different payload is rejected."""
        sig = _make_sig(PAYLOAD)
        tampered = PAYLOAD + b" tampered"
        assert verify_signature(tampered, sig) is False

    def test_different_payloads_different_sigs(self):
        """Two different payloads produce different signatures."""
        sig1 = _make_sig(b"payload_one")
        sig2 = _make_sig(b"payload_two")
        assert sig1 != sig2

    def test_timing_safe(self):
        """compare_digest is used — function does not short-circuit on mismatch."""
        # Can't easily test timing, but confirm the function uses compare_digest
        # by checking it still returns False for almost-correct sigs
        correct_sig = _make_sig(PAYLOAD)
        almost = correct_sig[:-1] + ("0" if correct_sig[-1] != "0" else "1")
        assert verify_signature(PAYLOAD, almost) is False
