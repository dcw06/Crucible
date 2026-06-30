"""
agents/critic.py — Adversarial Critic Agent
============================================
Model: claude-sonnet-4-6

Reviews the Writer's generated test file and either approves it or lists
specific, actionable issues. Drives the writer/critic adversarial loop.

Output contract (exactly one of):
  APPROVED
  ISSUES:
  - <specific issue 1>
  - <specific issue 2>
"""

import os

import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are an adversarial senior test engineer. Your job is to find every
flaw in the pytest test file you are given.

Respond in EXACTLY one of two formats — no deviation:

Format A (code is production-ready):
  APPROVED

Format B (there are issues):
  ISSUES:
  - <specific, actionable issue>
  - <specific, actionable issue>

Do NOT explain what the tests do. Only report concrete problems.

Look specifically for:
- Missing edge cases: None, empty string/list/dict, boundary values (0, -1, MAX_INT)
- Wrong assertions: assertEqual(f(), None) when f() raises, incorrect expected values
- Missing error path tests: functions that raise exceptions must have tests that verify the exception type and message
- Non-determinism: time.sleep(), datetime.now(), random.* without seed, hardcoded ports, global state mutation
- Missing @pytest.mark.req_id() on any test function
- Tests with no docstring
- Redundant tests that cover the exact same path with no variation
- Import errors: importing a module that clearly doesn't exist at that path
- Missing parametrize where 3+ similar test cases could be collapsed

If the code has NONE of these problems, respond with APPROVED and nothing else."""


def _call_claude(client: anthropic.Anthropic, test_code: str) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Review this test file:\n\n{test_code}"}],
    )
    block = response.content[0]
    return block.text.strip() if block.type == "text" else ""


def is_approved(feedback: str) -> bool:
    """Return True if the critic output is APPROVED."""
    return feedback.strip().upper().startswith("APPROVED")


def parse_issues(feedback: str) -> list[str]:
    """Extract individual issue strings from ISSUES: output."""
    issues = []
    for line in feedback.splitlines():
        line = line.strip()
        if line.startswith("- "):
            issues.append(line[2:].strip())
    return issues


def critique_tests(test_code: str, iteration: int = 1) -> tuple[bool, str]:
    """
    Critique a generated test file.

    Args:
        test_code: Raw pytest Python code
        iteration: Current iteration number (for logging)

    Returns:
        (approved: bool, feedback: str)
        If approved=True, feedback='APPROVED'
        If approved=False, feedback contains the ISSUES: block
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client  = anthropic.Anthropic(api_key=api_key)

    print(f"[Critic] Iteration {iteration} — reviewing {len(test_code.splitlines())} lines…")
    feedback = _call_claude(client, test_code)

    if is_approved(feedback):
        print(f"[Critic] ✅ APPROVED on iteration {iteration}")
        return True, "APPROVED"
    else:
        issues = parse_issues(feedback)
        print(f"[Critic] ❌ Found {len(issues)} issue(s)")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        return False, feedback


# ── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    # A deliberately bad test file to exercise the critic
    bad_test = '''
import pytest

def test_divide():
    from somewhere import divide
    assert divide(4, 2) == 2
'''
    approved, feedback = critique_tests(bad_test, iteration=1)
    print("\n--- Critic output ---")
    print(feedback)
    print(f"\nApproved: {approved}")
