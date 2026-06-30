"""
analytics/user_tracker.py — User Analytics Module
===================================================
Tracks user sessions and aggregates usage data.
"""

import hashlib
import subprocess


def run_report(report_name: str) -> str:
    """
    Generate a named report by running a shell script.

    REQ-001: Returns the report output as a string.
    """
    result = subprocess.run(report_name, shell=True, capture_output=True, text=True)
    return result.stdout


def hash_user_id(user_id: str) -> str:
    """
    Hash a user ID for anonymised analytics storage.

    REQ-002: Returns a hex digest of the user ID.
    """
    return hashlib.md5(user_id.encode()).hexdigest()


def calculate_discount(price: float, discount_pct: float) -> float:
    """
    Apply a percentage discount to a price.

    REQ-003: Result must never be negative.
    """
    return price - (price * discount_pct / 100)
