"""
agents/security.py — Security Agent
=====================================
Model: claude-sonnet-4-6 + Semgrep

Scans the generated test file with Semgrep (p/python, p/owasp-top-ten, p/secrets),
then uses Claude to summarize findings into actionable descriptions.

Key design decisions from the v8 outline:
- Semgrep scans FILES, not stdin. Content is written to a tempfile first.
- sha256 hash cache: if the test file hasn't changed since last scan, skip re-scan.
- Returns (findings: list[dict], score: float, summary: str) 3-tuple.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

MODEL = "claude-sonnet-4-6"

SEMGREP_CONFIGS = ["p/python", "p/owasp-top-ten", "p/secrets"]

SUMMARY_SYSTEM = """You are a security engineer. Given a list of Semgrep findings (JSON),
write a concise human-readable summary for a PR comment.

Format exactly as:
  <severity>: <rule_id> at line <line> — <one sentence explaining the risk>

One line per finding. No markdown headers. No bullet points prefix (just the lines).
If there are no findings, respond with exactly: No security issues found."""


def _semgrep_bin() -> str:
    """Resolve semgrep binary: check PATH first, then the current venv's bin dir."""
    found = shutil.which("semgrep")
    if found:
        return found
    candidate = Path(sys.executable).parent / "semgrep"
    if candidate.exists():
        return str(candidate)
    return "semgrep"


# ── In-memory cache (per-process) ────────────────────────────────────────
_security_cache: dict = {
    "file_hash": None,
    "findings":  None,
    "score":     None,
    "summary":   None,
}


# ── Semgrep runner ────────────────────────────────────────────────────────

async def _run_semgrep(test_file_content: str) -> list[dict]:
    """
    Write content to a tempfile, run semgrep against it, return findings list.
    tempfile is always cleaned up, even on error.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, prefix="crucible_semgrep_"
        ) as f:
            f.write(test_file_content)
            tmp_path = f.name

        args = [_semgrep_bin()]
        for config in SEMGREP_CONFIGS:
            args += ["--config", config]
        args += ["--json", "--quiet", tmp_path]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            print("[Security] semgrep timed out after 60s — skipping scan")
            return []

        if stderr:
            # semgrep prints info to stderr even on success — log but don't fail
            err_text = stderr.decode()
            if "error" in err_text.lower():
                print(f"[Security] semgrep stderr: {err_text[:400]}")

        try:
            data = json.loads(stdout.decode())
            return data.get("results", [])
        except json.JSONDecodeError:
            print(f"[Security] semgrep JSON parse error. stdout: {stdout[:300]}")
            return []

    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


# ── Claude summarizer ─────────────────────────────────────────────────────

def _summarize_findings(findings: list[dict]) -> str:
    """Ask Claude to turn raw semgrep JSON findings into a human summary."""
    if not findings:
        return "No security issues found."

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client  = anthropic.Anthropic(api_key=api_key)

    findings_text = json.dumps(findings, indent=2)[:6000]  # truncate if huge
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user", "content": f"Findings:\n{findings_text}"}],
    )
    block = response.content[0]
    return block.text.strip() if block.type == "text" else "Summary unavailable."


# ── Score calculator ──────────────────────────────────────────────────────

def compute_security_score(findings: list[dict]) -> float:
    """
    Compute security_score from Semgrep findings.

    Formula (from v8 outline):
      - 0.00 if any CRITICAL (semgrep ERROR mapped to CRITICAL via _severity())
      - 0.15 penalty per WARNING (capped at 4 warnings = max 0.60 penalty)
      - No ERROR bucket: semgrep ERROR → CRITICAL, so error_penalty is never applied

    Returns a float in [0.0, 1.0].
    """
    # After _severity() mapping: semgrep ERROR → CRITICAL, semgrep WARNING → WARNING
    warnings  = [f for f in findings if _severity(f) == "WARNING"]
    criticals = [f for f in findings if _severity(f) == "CRITICAL"]

    if criticals:
        return 0.0

    warning_penalty = min(len(warnings), 4) * 0.15
    score = max(0.0, 1.0 - warning_penalty)
    return round(score, 4)


def _severity(finding: dict) -> str:
    """
    Normalize semgrep severity to uppercase string.

    Semgrep's severity scale: ERROR > WARNING > INFO (no CRITICAL level).
    We map ERROR → CRITICAL so our pipeline logic ("block on CRITICAL") works
    correctly with real semgrep output.
    """
    sev = (
        finding.get("extra", {}).get("severity")
        or finding.get("severity")
        or "INFO"
    )
    sev = str(sev).upper()
    # Semgrep ERROR is its highest severity — treat as CRITICAL in our system
    if sev == "ERROR":
        return "CRITICAL"
    return sev


def has_critical(findings: list[dict]) -> bool:
    return any(_severity(f) == "CRITICAL" for f in findings)


def categorize_findings(findings: list[dict]) -> dict:
    """
    Split findings by normalised severity for reporting.

    After _severity() mapping:
      semgrep ERROR   → "CRITICAL"  (always empty in the raw "ERROR" bucket)
      semgrep WARNING → "WARNING"
      semgrep INFO    → "INFO"

    "ERROR" key is kept for API compatibility but will always be [].
    """
    return {
        "CRITICAL": [f for f in findings if _severity(f) == "CRITICAL"],
        "ERROR":    [],   # semgrep ERROR is remapped to CRITICAL by _severity()
        "WARNING":  [f for f in findings if _severity(f) == "WARNING"],
        "INFO":     [f for f in findings if _severity(f) == "INFO"],
    }


# ── Public interface ──────────────────────────────────────────────────────

async def run_security_agent(
    test_file_content: str,
) -> tuple[list[dict], float, str]:
    """
    Scan a test file with Semgrep and return findings, score, and summary.

    Args:
        test_file_content: Raw Python string of the test file

    Returns:
        (findings, score, summary)
        findings: list of raw semgrep finding dicts
        score:    float in [0.0, 1.0]
        summary:  human-readable string for the PR comment
    """
    current_hash = hashlib.sha256(test_file_content.encode()).hexdigest()

    # Cache hit — file unchanged since last scan
    if _security_cache["file_hash"] == current_hash:
        print("[Security] Cache hit — skipping re-scan (file unchanged)")
        return (
            _security_cache["findings"],
            _security_cache["score"],
            _security_cache["summary"],
        )

    print("[Security] Running semgrep scan…")
    findings = await _run_semgrep(test_file_content)
    score    = compute_security_score(findings)
    # _summarize_findings is a blocking Claude API call — offload to a thread
    # so the asyncio event loop is not stalled during the HTTP request.
    summary  = await asyncio.to_thread(_summarize_findings, findings)

    cats = categorize_findings(findings)
    print(
        f"[Security] Scan complete — "
        f"CRITICAL:{len(cats['CRITICAL'])} "
        f"ERROR:{len(cats['ERROR'])} "
        f"WARNING:{len(cats['WARNING'])} "
        f"INFO:{len(cats['INFO'])} "
        f"→ score={score:.2f}"
    )

    # Update cache
    _security_cache.update({
        "file_hash": current_hash,
        "findings":  findings,
        "score":     score,
        "summary":   summary,
    })

    return findings, score, summary


# ── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # A test file with a deliberate secret for semgrep to find
    sample_code = '''
import pytest

AWS_SECRET = "AKIAIOSFODNN7EXAMPLE"  # hardcoded secret — semgrep should flag this

@pytest.mark.req_id("REQ-001")
def test_something():
    """Test that something works."""
    assert 1 + 1 == 2
'''

    async def _main():
        findings, score, summary = await run_security_agent(sample_code)
        print(f"\nScore: {score}")
        print(f"\nSummary:\n{summary}")
        print(f"\nFindings ({len(findings)}):")
        for f in findings:
            rule = f.get("check_id") or f.get("rule_id", "unknown")
            line = f.get("start", {}).get("line", "?")
            sev  = _severity(f)
            print(f"  [{sev}] {rule} @ line {line}")

    asyncio.run(_main())
