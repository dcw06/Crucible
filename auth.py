"""
auth.py — HMAC webhook verification (no external dependencies)
=============================================================
Kept in its own module so unit tests can import it without
triggering the full pipeline/agents/anthropic import chain.
"""

import hashlib
import hmac
import os


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """
    Verify GitHub webhook HMAC-SHA256 signature.

    Uses positional args to avoid Python version compatibility issues:
        hmac.new(key, msg, digestmod)  — NOT hmac.new(key, msg=..., digestmod=...)

    Returns True (skip verification) if GITHUB_WEBHOOK_SECRET is not set,
    so dev environments without a secret configured don't break.
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        print("[Auth] ⚠️  GITHUB_WEBHOOK_SECRET not set — skipping HMAC verification")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(),
        payload_body,       # positional — not msg=payload_body
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature_header)
