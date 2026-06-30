"""
tests/test_confidence.py — Confidence score formula unit tests
==============================================================
Verifies the v8 formula is implemented correctly, including:
- Demo math: iteration=2, tc_pass_rate=0.25, CRITICAL → 0.37 ✅
- Continuity: no special case at max iterations
- Security score cap: warnings capped at 4
- Fallback formula: tc_pass_rate=None renormalizes correctly
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from confidence import compute_confidence, compute_critic_score, compute_security_score


class TestCriticScore:
    def test_iteration_1(self):
        assert compute_critic_score(1) == 1.0

    def test_iteration_2(self):
        assert compute_critic_score(2) == 0.9

    def test_iteration_3(self):
        assert compute_critic_score(3) == 0.8

    def test_iteration_4(self):
        assert compute_critic_score(4) == 0.7

    def test_iteration_5(self):
        assert compute_critic_score(5) == 0.6

    def test_floor_at_high_iteration(self):
        """Score is floored at 0.5 — never goes below."""
        assert compute_critic_score(10) == 0.5
        assert compute_critic_score(100) == 0.5

    def test_continuous_no_special_case(self):
        """Iteration 5 (0.6) should be higher than floor (0.5) — no discontinuity."""
        assert compute_critic_score(5) > 0.5


class TestSecurityScore:
    def test_no_findings(self):
        assert compute_security_score(0, 0, False) == 1.0

    def test_one_warning(self):
        assert compute_security_score(1, 0, False) == 0.85

    def test_two_warnings(self):
        assert compute_security_score(2, 0, False) == 0.70

    def test_three_warnings(self):
        assert compute_security_score(3, 0, False) == 0.55

    def test_four_warnings_floor(self):
        """4+ warnings floor at 0.40 — separated from CRITICAL."""
        assert compute_security_score(4, 0, False) == 0.40

    def test_many_warnings_still_floor(self):
        """7 warnings should not go below 0.40 (the cap prevents it)."""
        assert compute_security_score(7, 0, False) == 0.40

    def test_one_error(self):
        assert compute_security_score(0, 1, False) == 0.50

    def test_critical_always_zero(self):
        """CRITICAL overrides everything — score is always 0.0."""
        assert compute_security_score(0, 0, True) == 0.0
        assert compute_security_score(100, 100, True) == 0.0

    def test_warnings_and_errors_no_critical(self):
        score = compute_security_score(2, 1, False)
        assert score == pytest.approx(max(0.0, 1.0 - min(2, 4) * 0.15 - 1 * 0.5))

    def test_floor_at_zero(self):
        """Score never goes below 0.0."""
        assert compute_security_score(4, 5, False) == 0.0


class TestComputeConfidence:
    def test_demo_math(self):
        """
        Demo scenario from v8 outline must produce exactly 0.37.
        critic=0.9, security=0.0 (CRITICAL), tc=0.25
        → 0.3×0.9 + 0.3×0.0 + 0.4×0.25 = 0.27 + 0 + 0.10 = 0.37
        """
        result = compute_confidence(
            iteration=2, warnings=2, errors=0,
            any_critical=True, tc_pass_rate=0.25
        )
        assert result.confidence     == 0.37
        assert result.critic_score   == 0.9
        assert result.security_score == 0.0
        assert result.tc_pass_rate   == 0.25

    def test_tc_pass_rate_none_renormalizes(self):
        """When tc_pass_rate is None, formula is 0.6×critic + 0.4×security."""
        result = compute_confidence(
            iteration=1, warnings=0, errors=0,
            any_critical=False, tc_pass_rate=None
        )
        expected = round(0.6 * 1.0 + 0.4 * 1.0, 2)
        assert result.confidence == expected
        assert "Pending" in result.tc_label

    def test_perfect_score(self):
        """Iteration 1, no findings, 100% TC pass rate → confidence = 1.0."""
        result = compute_confidence(
            iteration=1, warnings=0, errors=0,
            any_critical=False, tc_pass_rate=1.0
        )
        assert result.confidence == 1.0

    def test_gate_approved(self):
        result = compute_confidence(1, 0, 0, False, 1.0)
        assert result.gate == "APPROVED"

    def test_gate_review(self):
        # critic=0.8, security=0.85, tc=0.7
        # → 0.3×0.8 + 0.3×0.85 + 0.4×0.7 = 0.24 + 0.255 + 0.28 = 0.775
        result = compute_confidence(3, 1, 0, False, 0.7)
        assert result.confidence == pytest.approx(0.775, abs=0.01)
        assert result.gate == "REVIEW"

    def test_gate_blocked_critical(self):
        """CRITICAL always blocks regardless of score."""
        result = compute_confidence(1, 0, 0, True, 1.0)
        assert result.gate == "BLOCKED"

    def test_weights_sum_to_one(self):
        """With tc_pass_rate provided, weights 0.3+0.3+0.4=1.0."""
        result = compute_confidence(1, 0, 0, False, 0.5)
        expected = round(0.3 * 1.0 + 0.3 * 1.0 + 0.4 * 0.5, 2)
        assert result.confidence == expected
