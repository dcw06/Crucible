"""
confidence.py — Confidence Scorer + PR Comment Formatter
=========================================================
Implements the v8 formula exactly:

  confidence = 0.3 × critic_score + 0.3 × security_score + 0.4 × tc_pass_rate

With:
  - critic_score  = max(0.5, 1.1 − 0.1 × iteration)  [continuous, no special case]
  - security_score = 0.0 if CRITICAL else max(0.0, 1.0 − min(warnings,4)×0.15 − errors×0.5)
  - tc_pass_rate  = None → renormalize to 0.6/0.4 split (TC excluded)

Demo verified:
  iteration=2, tc_pass_rate=0.25, any_critical=True
  → critic=0.9, security=0.0, confidence=0.3×0.9+0+0.4×0.25 = 0.37 ✅
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class ConfidenceResult:
    confidence:    float
    critic_score:  float
    security_score: float
    tc_pass_rate:  float | None
    tc_label:      str
    iteration:     int
    warning_count: int
    error_count:   int
    any_critical:  bool
    fragile_count: int = 0
    req_coverage:  dict = field(default_factory=dict)  # {req_id: test_count}

    @property
    def gate(self) -> str:
        if self.any_critical:
            return "BLOCKED"
        if self.confidence >= 0.85:
            return "APPROVED"
        if self.confidence >= 0.65:
            return "REVIEW"
        return "BLOCKED"

    @property
    def gate_emoji(self) -> str:
        return {"APPROVED": "✅", "REVIEW": "⚠️", "BLOCKED": "❌"}[self.gate]


# ── Score computation ──────────────────────────────────────────────────────

def compute_critic_score(iteration: int) -> float:
    """
    Continuous, floored formula — no special case for max iterations.
    iteration 1→1.0, 2→0.9, 3→0.8, 4→0.7, 5→0.6, floor 0.5
    """
    return round(max(0.5, 1.1 - 0.1 * iteration), 4)


def compute_security_score(
    warnings: int,
    errors: int,
    any_critical: bool,
) -> float:
    """
    Warning cap at 4 prevents conflating warning-heavy with CRITICAL.
    security_score table:
      0 findings  → 1.00
      1 warning   → 0.85
      2 warnings  → 0.70
      3 warnings  → 0.55
      4+ warnings → 0.40  (floor for warnings alone)
      1 error     → 0.50
      CRITICAL    → 0.00  (unambiguous)
    """
    if any_critical:
        return 0.0
    warning_penalty = min(warnings, 4) * 0.15
    # error_penalty: parameter kept for API completeness.
    # In practice, pipeline always passes errors=0 because security.py maps
    # semgrep ERROR → CRITICAL via _severity(), so the ERROR bucket is always empty.
    error_penalty   = errors * 0.50
    return round(max(0.0, 1.0 - warning_penalty - error_penalty), 4)


def compute_confidence(
    iteration: int,
    warnings: int,
    errors: int,
    any_critical: bool,
    tc_pass_rate: float | None,
    fragile_count: int = 0,
    req_coverage: dict | None = None,
) -> ConfidenceResult:
    """
    Compute the full confidence result.

    Two formula modes:
      Normal:   0.3×critic + 0.3×security + 0.4×tc_pass_rate
      Fallback: 0.6×critic + 0.4×security  (TC excluded, renormalized)
    """
    critic_score   = compute_critic_score(iteration)
    security_score = compute_security_score(warnings, errors, any_critical)

    if tc_pass_rate is not None:
        confidence = (
            0.3 * critic_score
            + 0.3 * security_score
            + 0.4 * tc_pass_rate
        )
        tc_label = f"{tc_pass_rate:.0%} TC pass rate"
    else:
        confidence = 0.6 * critic_score + 0.4 * security_score
        tc_label   = "⚠️ TC Pending — score excludes execution data"

    return ConfidenceResult(
        confidence     = round(confidence, 2),
        critic_score   = critic_score,
        security_score = security_score,
        tc_pass_rate   = tc_pass_rate,
        tc_label       = tc_label,
        iteration      = iteration,
        warning_count  = warnings,
        error_count    = errors,
        any_critical   = any_critical,
        fragile_count  = fragile_count,
        req_coverage   = req_coverage or {},
    )


# ── PR comment formatter ───────────────────────────────────────────────────

def format_pr_comment(
    result: ConfidenceResult,
    pr_number: int,
    security_summary: str = "",
    maestro_case_id: str | None = None,
) -> str:
    """
    Format the GitHub PR comment posted after the pipeline completes.
    Handles all tc_pass_rate states gracefully.
    """
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────────────
    lines.append(f"## {result.gate_emoji} Crucible — PR #{pr_number}")
    lines.append("")

    # ── Confidence score ──────────────────────────────────────────────────
    bar_filled = int(result.confidence * 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    lines.append(f"**Confidence Score: `{result.confidence:.2f}`** `[{bar}]`")
    lines.append("")

    # ── Gate decision ─────────────────────────────────────────────────────
    gate_messages = {
        "APPROVED": "✅ Score ≥ 0.85 — safe to merge.",
        "REVIEW":   "⚠️ Score 0.65–0.84 — human review recommended.",
        "BLOCKED":  "❌ Score < 0.65 or CRITICAL finding — merge blocked.",
    }
    lines.append(f"> {gate_messages[result.gate]}")
    lines.append("")

    # ── Score breakdown ───────────────────────────────────────────────────
    lines.append("### Score Breakdown")
    lines.append("")
    lines.append("| Component | Value | Weight |")
    lines.append("|---|---|---|")
    lines.append(
        f"| Critic (iteration {result.iteration}) "
        f"| `{result.critic_score:.2f}` | 30% |"
    )
    lines.append(
        f"| Security "
        f"| `{result.security_score:.2f}` | 30% |"
    )

    if result.tc_pass_rate is not None:
        lines.append(
            f"| Test Cloud pass rate "
            f"| `{result.tc_pass_rate:.0%}` | 40% |"
        )
    else:
        lines.append(
            "| Test Cloud pass rate "
            "| `—` | ~~40%~~ renormalized |"
        )
    lines.append("")
    # Surface tc_label when TC data is unavailable — it carries the human-readable notice
    if result.tc_pass_rate is None:
        lines.append(f"> {result.tc_label}")
        lines.append("")

    # ── Security findings ─────────────────────────────────────────────────
    lines.append("### Security")
    lines.append("")
    if result.any_critical:
        lines.append("🚨 **CRITICAL finding — merge blocked regardless of score.**")
        lines.append("")
    if security_summary and security_summary != "No security issues found.":
        lines.append("```")
        lines.append(security_summary)
        lines.append("```")
    else:
        lines.append("✅ No security issues found.")
    lines.append("")

    # ── FRAGILE tests ─────────────────────────────────────────────────────
    if result.fragile_count > 0:
        lines.append(
            f"### ⚡ FRAGILE Tests: {result.fragile_count} flagged"
        )
        lines.append("")
        lines.append(
            "_These tests contain non-deterministic patterns "
            "(time.sleep, datetime.now, hardcoded ports, etc.) "
            "and have been tagged `[FRAGILE]` in Test Cloud._"
        )
        lines.append("")

    # ── Requirements coverage ─────────────────────────────────────────────
    if result.req_coverage:
        lines.append("### Requirements Coverage")
        lines.append("")
        lines.append("| Req ID | Tests Generated | TC Result |")
        lines.append("|---|---|---|")
        for req_id, count in sorted(result.req_coverage.items()):
            if result.tc_pass_rate is None:
                tc_status = "—"
            elif result.tc_pass_rate > 0.5:
                tc_status = "✅"
            else:
                tc_status = "❌"
            lines.append(f"| `{req_id}` | {count} | {tc_status} |")
        lines.append("")

    # ── Maestro case ──────────────────────────────────────────────────────
    if maestro_case_id:
        lines.append(f"### 🔔 Maestro Case Opened")
        lines.append("")
        lines.append(
            f"Case `{maestro_case_id}` has been opened for human review "
            f"due to a CRITICAL security finding."
        )
        lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(
        "_🤖 Generated by [Crucible](https://github.com) "
        "— Built with [Claude Code](https://claude.ai/code)_"
    )

    return "\n".join(lines)


# ── Verification (demo math) ───────────────────────────────────────────────

def _verify_demo_math():
    """
    Verify the demo scenario from the v8 outline produces 0.37.
    Run this to confirm formula correctness before demo day.
    """
    result = compute_confidence(
        iteration=2,
        warnings=2,
        errors=0,
        any_critical=True,    # CRITICAL → security_score=0.0
        tc_pass_rate=0.25,    # staged demo payload
    )
    assert result.critic_score   == 0.9,  f"critic_score={result.critic_score}"
    assert result.security_score == 0.0,  f"security_score={result.security_score}"
    assert result.confidence     == 0.37, f"confidence={result.confidence}"
    print("✅ Demo math verified: confidence=0.37 ✓")
    return result


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Run verification
    result = _verify_demo_math()

    # Show what the PR comment would look like
    comment = format_pr_comment(
        result,
        pr_number=42,
        security_summary="CRITICAL: hardcoded-secrets at line 14 — AWS key exposed in test file",
        maestro_case_id="CASE-00142",
    )
    print("\n" + "─" * 60)
    print("SAMPLE PR COMMENT:")
    print("─" * 60)
    print(comment)
