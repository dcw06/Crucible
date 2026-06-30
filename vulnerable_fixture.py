"""
vulnerable_fixture.py — Crucible Demo Target
=============================================
Pre-built fixture for the hackathon demo.

Designed so the pipeline produces EXACTLY the demo scenario from §14:
  - Writer needs exactly 2 iterations to get APPROVED
  - Security scan finds 1 CRITICAL (semgrep ERROR) + 2 WARNINGs
  - 2 FRAGILE patterns present (to trigger [FRAGILE] flagging)
  - 3 linked requirements (REQ-001, REQ-002, REQ-003)
  - tc_pass_rate = 0.25 when tests run against this code (1 of 4 passes)

Semgrep severity mapping in our system:
  semgrep ERROR   → CRITICAL (blocks pipeline, triggers Maestro)
  semgrep WARNING → WARNING  (penalty to confidence score)

Expected findings:
  [CRITICAL] subprocess-shell-true    — shell=True with user input (ERROR level)
  [WARNING]  insecure-hash-algorithm-md5                           (WARNING level)
  [WARNING]  md5-used-as-password                                  (WARNING level)

Requirements covered:
  REQ-001: run_user_command() returns command output as a string
  REQ-002: hash_password() returns a hex digest (MD5 — intentionally insecure for demo)
  REQ-003: calculate_discount() never returns a negative price

⚠️  This file is INTENTIONALLY VULNERABLE for demo purposes.
    Do not use in production.
"""

import hashlib
import subprocess
import time
import datetime

# ── CRITICAL (semgrep ERROR): subprocess with shell=True + user input ─────
# semgrep rule: python.lang.security.audit.subprocess-shell-true
# Severity: ERROR → mapped to CRITICAL in our pipeline
def run_user_command(user_input: str) -> str:
    """
    Execute a user-provided command.

    REQ-001: Returns command output as a string.

    Bug: shell=True with unsanitized user input — command injection.
    This is the CRITICAL finding that blocks the merge and opens a Maestro case.
    """
    result = subprocess.run(user_input, shell=True, capture_output=True, text=True)
    return result.stdout


# ── WARNING 1: Weak hash algorithm ───────────────────────────────────────
# semgrep rule: python.lang.security.insecure-hash-algorithms-md5
def hash_password(password: str) -> str:
    """
    Hash a password for storage.
    Bug: Uses MD5 — cryptographically broken, not suitable for passwords.
    """
    return hashlib.md5(password.encode()).hexdigest()   # WARNING: MD5 insecure


# ── WARNING 2: MD5 used as password hash (same line, two rules fire) ──────
# semgrep rule: python.lang.security.audit.md5-used-as-password
# Both MD5 rules fire on the same line — gives us our 2 WARNINGs


# ── FRAGILE pattern 1: time.sleep() ──────────────────────────────────────
def wait_for_result(timeout: int = 5) -> bool:
    """
    Wait for an external result.
    FRAGILE: Uses time.sleep() — non-deterministic in CI.
    """
    time.sleep(timeout)   # FRAGILE: time.sleep
    return True


# ── FRAGILE pattern 2: datetime.now() ────────────────────────────────────
def get_report_timestamp() -> str:
    """
    Return a formatted timestamp for reports.
    FRAGILE: Uses datetime.now() — non-deterministic.
    """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")   # FRAGILE: datetime.now


# ── Logic bug: negative discount ─────────────────────────────────────────
def calculate_discount(price: float, discount_pct: float) -> float:
    """
    Apply a percentage discount to a price.

    REQ-003: Result must never be negative.

    Bug: No floor at 0 — a 150% discount returns a negative price.

    Examples:
      calculate_discount(100.0, 10)   → 90.0   ✅
      calculate_discount(100.0, 100)  → 0.0    ✅
      calculate_discount(100.0, 150)  → -50.0  ❌  (violates REQ-003)
    """
    return price - (price * discount_pct / 100)


# ── Edge case: integer division ───────────────────────────────────────────
def safe_divide(numerator: float, denominator: float) -> float:
    """
    Divide numerator by denominator.
    Bug: No guard for denominator=0 — raises ZeroDivisionError instead of
    a clean ValueError with a helpful message.
    """
    return numerator / denominator


# ── Missing type guard ────────────────────────────────────────────────────
def format_user_greeting(name) -> str:
    """
    Return a personalised greeting.
    Bug: No type check — format_user_greeting(None) raises AttributeError.
    """
    return f"Hello, {name.strip()}!"
# demo
# demo
