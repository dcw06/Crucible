"""
agents/writer.py — Test Writer Agent
=====================================
Model: claude-sonnet-4-6

Generates a pytest test file from a PR diff and requirement IDs.
Runs inside the writer/critic adversarial loop (up to MAX_ITERATIONS).

Returns raw Python only — no markdown, no fences, no explanation.
"""

import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

MODEL          = "claude-sonnet-4-6"
MAX_ITERATIONS = 5

SYSTEM_PROMPT = """You are an expert test engineer specializing in pytest.
Given a code diff and a list of requirements, write a complete pytest test file.

Rules — follow all of them strictly:
1. Return ONLY raw Python code. No markdown. No triple-backtick fences. No explanation.
2. Tag every test function with @pytest.mark.req_id("<id>") using the req_id from the requirements list.
3. Cover: happy paths, edge cases (None, empty, boundary values), error paths (exceptions), type coercions.
4. Use pytest.mark.parametrize for data-driven cases.
5. Each test must have a docstring: one sentence explaining what it verifies.
6. Import only from the standard library and pytest — do not import the module under test unless the diff is provided with its full path.
7. If you cannot determine the module path from the diff, use a mock or stub.
8. Never use time.sleep(), datetime.now(), random without a seed, hardcoded ports, or global state — these are FRAGILE and will be flagged."""


def _build_prompt(
    diff: str,
    requirements: list[dict],
    previous_code: str | None = None,
    feedback: str | None = None,
    failing_tests: list[dict] | None = None,
) -> str:
    req_block = "\n".join(
        f"  - {r['req_id']} [{r['priority']}]: {r['description']}"
        for r in requirements
    )

    # Bidirectional TC integration: surface currently-failing TC tests so the
    # Writer knows which scenarios need the most attention on iteration 1.
    failing_block = ""
    if failing_tests:
        names = [t.get("name", "unknown") for t in failing_tests]
        failing_block = (
            "\n\nCurrently failing Test Cloud tests (prioritize coverage of these):\n"
            + "\n".join(f"  - {n}" for n in names)
        )

    if previous_code and feedback:
        return (
            f"Requirements to cover:\n{req_block}{failing_block}\n\n"
            f"Code diff:\n```\n{diff}\n```\n\n"
            f"Issues found by the critic in your last attempt:\n{feedback}\n\n"
            f"Previous test file (fix all issues above):\n{previous_code}\n\n"
            "Return only the corrected raw Python test file."
        )
    else:
        return (
            f"Requirements to cover:\n{req_block}{failing_block}\n\n"
            f"Code diff:\n```\n{diff}\n```\n\n"
            "Write a complete pytest test file. "
            "Tag every test with @pytest.mark.req_id(). "
            "Return only raw Python."
        )


def _strip_fences(code: str) -> str:
    """Remove accidental markdown fences if the model adds them."""
    code = re.sub(r"^```[\w]*\n?", "", code, flags=re.MULTILINE)
    code = re.sub(r"^```\s*$",     "", code, flags=re.MULTILINE)
    return code.strip()


def _call_claude(
    client: anthropic.Anthropic,
    diff: str,
    requirements: list[dict],
    previous_code: str | None,
    feedback: str | None,
    failing_tests: list[dict] | None = None,
) -> str:
    prompt = _build_prompt(diff, requirements, previous_code, feedback, failing_tests)
    response = client.messages.create(
        model=MODEL,
        max_tokens=8096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    block = response.content[0]
    raw = block.text.strip() if block.type == "text" else ""
    return _strip_fences(raw)


def write_tests(
    diff: str,
    requirements: list[dict],
    previous_code: str | None = None,
    feedback: str | None = None,
    iteration: int = 1,
    failing_tests: list[dict] | None = None,
) -> str:
    """
    Generate (or revise) a pytest test file.

    Args:
        diff:          The PR diff text
        requirements:  List of {req_id, description, priority} dicts
        previous_code: The previous iteration's test file (None on first call)
        feedback:      Critic's ISSUES output (None on first call)
        iteration:     Current iteration number (for logging)
        failing_tests: Currently failing TC tests (passed on iteration 1 only);
                       informs the Writer which scenarios to prioritise.

    Returns:
        Raw Python string — a complete pytest file
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client  = anthropic.Anthropic(api_key=api_key)

    print(f"[Writer] Iteration {iteration}/{MAX_ITERATIONS} — generating tests…")
    code = _call_claude(client, diff, requirements, previous_code, feedback, failing_tests)
    print(f"[Writer] Generated {len(code.splitlines())} lines")
    return code


# ── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_diff = """
+def divide(a, b):
+    \"\"\"Divide a by b. Raises ValueError on division by zero.\"\"\"
+    if b == 0:
+        raise ValueError("Cannot divide by zero")
+    return a / b
    """
    sample_reqs = [
        {"req_id": "REQ-001", "description": "divide() raises ValueError when b=0",     "priority": "HIGH"},
        {"req_id": "REQ-002", "description": "divide() returns correct float for valid inputs", "priority": "HIGH"},
        {"req_id": "REQ-003", "description": "divide() handles negative numbers",        "priority": "MEDIUM"},
    ]
    code = write_tests(sample_diff, sample_reqs, iteration=1)
    print("\n--- Generated test file ---")
    print(code)
