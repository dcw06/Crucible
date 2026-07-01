"""
pipeline.py — Crucible Pipeline Orchestrator
=============================================
Runs the full multi-agent pipeline for a single PR:

  Step 1: Fetch PR files (already done by webhook handler)
  Step 2: Requirements analysis
  Step 3-5: Writer/Critic adversarial loop (+ sequential Security scan per iteration)
  Step 6: pytest execution → JUnit XML
  Step 7: TC upload + trigger + poll
  Step 8: Confidence score + PR comment
  Step 9: Maestro case (if CRITICAL)

Called by the background worker in main.py — never blocks the event loop.
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

from agents import (
    analyze_requirements,
    write_tests,
    critique_tests,
    run_security_agent,
    MAX_ITERATIONS,
)
from agents.security import has_critical, categorize_findings
from confidence import compute_confidence, format_pr_comment
from tc_client import full_tc_roundtrip, get_failing_tests
from state import update_step, upsert_run

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "")
MAESTRO_BASE = os.environ.get("MAESTRO_BASE_URL", "").rstrip("/")
MAESTRO_TOKEN = os.environ.get("MAESTRO_TOKEN", "")

def _resolve_bin(name: str) -> str:
    """Resolve a binary: check PATH first, then the current venv's bin dir."""
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    return name


FRAGILE_PATTERNS = [
    r"time\.sleep\(",
    r"datetime\.now\(",
    r"random\.",
    r"localhost:\d{4,5}",
    r"global\s+\w+",
    r"os\.environ\[",
]


def _detect_fragile(test_code: str) -> int:
    """Count how many FRAGILE pattern matches exist in the test file."""
    return sum(
        len(re.findall(p, test_code))
        for p in FRAGILE_PATTERNS
    )


def _extract_docstrings(files: list[dict]) -> str:
    """Pull docstrings/comments from changed files for requirements analysis."""
    chunks = []
    for f in files:
        patch = f.get("patch", "")
        # grab added lines that look like docstrings or comments
        doc_lines = [
            line[1:] for line in patch.splitlines()
            if line.startswith("+") and ('"""' in line or "'''" in line or line.strip().startswith("#"))
        ]
        if doc_lines:
            chunks.append(f"# {f.get('filename', 'unknown')}\n" + "\n".join(doc_lines))
    return "\n\n".join(chunks)


def _build_diff(files: list[dict]) -> str:
    """Concatenate all file patches into a single diff string."""
    parts = []
    for f in files:
        filename = f.get("filename", "unknown")
        patch    = f.get("patch", "")
        if patch:
            parts.append(f"--- a/{filename}\n+++ b/{filename}\n{patch}")
    return "\n\n".join(parts)


def _extract_pr_source(files: list[dict]) -> str:
    """Extract added Python lines from the PR patches for security scanning."""
    chunks = []
    for f in files:
        if not f.get("filename", "").endswith(".py"):
            continue
        patch = f.get("patch", "")
        added = [
            line[1:]
            for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        if added:
            chunks.append(f"# {f['filename']}\n" + "\n".join(added))
    return "\n\n".join(chunks)


async def _post_github_comment(pr_number: int, body: str) -> None:
    """Post a comment to the GitHub PR."""
    missing = [v for v, val in [
        ("GITHUB_TOKEN", GITHUB_TOKEN),
        ("GITHUB_OWNER", GITHUB_OWNER),
        ("GITHUB_REPO",  GITHUB_REPO),
    ] if not val]
    if missing:
        print(f"[Pipeline] {', '.join(missing)} not set — skipping PR comment")
        return
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues/{pr_number}/comments"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github+json"},
            json={"body": body},
            timeout=15,
        )
    if resp.status_code not in (200, 201):
        print(f"[Pipeline] PR comment failed: HTTP {resp.status_code}")
    else:
        print(f"[Pipeline] PR comment posted to #{pr_number}")


async def _open_maestro_case(findings: list[dict], pr_number: int) -> str | None:
    """Open a Maestro case for CRITICAL security findings. Returns case ID or None."""
    if not MAESTRO_BASE or not MAESTRO_TOKEN:
        print("[Pipeline] Maestro not configured — logging CRITICAL finding locally")
        return None

    # Use has_critical() — it applies _severity() which maps semgrep ERROR → CRITICAL.
    # Checking raw semgrep severity for "CRITICAL" would always miss (semgrep uses "ERROR").
    if not has_critical(findings):
        return None

    # Find the first CRITICAL-equivalent finding for the case description
    # categorize_findings() applies _severity() mapping (semgrep ERROR → CRITICAL)
    cats = categorize_findings(findings)
    critical = cats["CRITICAL"]
    sample = critical[0]
    rule_id = sample.get("check_id") or sample.get("rule_id", "unknown")
    line    = sample.get("start", {}).get("line", "?")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MAESTRO_BASE}/api/v1/Maestro/Cases",
            headers={"Authorization": f"Bearer {MAESTRO_TOKEN}",
                     "Content-Type": "application/json"},
            json={
                "title": f"CRITICAL security finding in PR #{pr_number}",
                "severity": "HIGH",
                "description": (
                    f"Crucible detected a CRITICAL Semgrep finding.\n\n"
                    f"Rule: {rule_id}\nLine: {line}\n"
                    f"PR: #{pr_number}"
                ),
            },
            timeout=15,
        )

    if resp.status_code in (200, 201):
        data    = resp.json()
        case_id = data.get("id") or data.get("caseId", "CASE-UNKNOWN")
        print(f"[Pipeline] Maestro case opened: {case_id}")
        return str(case_id)
    else:
        print(f"[Pipeline] Maestro case failed: HTTP {resp.status_code} {resp.text[:200]}")
        return None


async def run_pipeline(files: list[dict], pr_number: int) -> None:
    """
    Full Crucible pipeline for one PR. Called by the asyncio.Queue worker.
    Updates SQLite state at each step for the Streamlit dashboard.
    """
    print(f"\n{'='*60}")
    print(f"[Pipeline] Starting PR #{pr_number}")
    print(f"{'='*60}")

    upsert_run(pr_number, step=1, step_label="Webhook received & verified")

    try:
        # ── Step 2: Get failing tests from TC (inbound) ───────────────────
        update_step(pr_number, 2, "Fetching current TC failures")
        async with httpx.AsyncClient() as client:
            failing_tests = await get_failing_tests(client)
        if failing_tests:
            print(f"[Pipeline] {len(failing_tests)} currently failing TC tests noted for Writer")

        # ── Step 3: Requirements analysis ─────────────────────────────────
        update_step(pr_number, 3, "Requirements analyzed")
        pr_description = f"PR #{pr_number} changes: " + ", ".join(
            f.get("filename", "") for f in files
        )
        docstrings = _extract_docstrings(files)
        requirements = analyze_requirements(
            pr_description=pr_description,
            docstrings=docstrings,
        )
        req_ids = [r["req_id"] for r in requirements]
        print(f"[Pipeline] Requirements: {req_ids}")

        # ── Steps 4–6: Writer / Critic adversarial loop ───────────────────
        diff = _build_diff(files)
        current_code: str | None = None
        feedback:     str | None = None
        approved      = False
        iteration     = 0

        # Security scan state
        findings:         list[dict] = []
        security_summary: str        = "No security issues found."

        for iteration in range(1, MAX_ITERATIONS + 1):
            # Writer
            update_step(pr_number, 4, f"Tests generated (Writer — iteration {iteration})")
            # Pass failing_tests on iteration 1 only — gives Writer TC context;
            # subsequent iterations are guided by the Critic's feedback instead.
            current_code = write_tests(
                diff, requirements, current_code, feedback, iteration,
                failing_tests=failing_tests if iteration == 1 else None,
            )

            # Critic
            update_step(pr_number, 5, f"Tests reviewed (Critic — iteration {iteration})")
            approved, feedback = critique_tests(current_code, iteration)

            # Security agent runs sequentially after critic review.
            # Scans both generated tests AND the PR source code — catches issues
            # in the code being merged, not just in the test file.
            # Hash cache skips re-scan if combined content is unchanged.
            update_step(pr_number, 6, "Security scanned (Semgrep)")
            pr_source = _extract_pr_source(files)
            scan_content = current_code + ("\n\n" + pr_source if pr_source else "")
            findings, _, security_summary = await run_security_agent(scan_content)

            if approved:
                break

        fragile_count = _detect_fragile(current_code or "")
        if fragile_count > 0:
            print(f"[Pipeline] ⚡ {fragile_count} FRAGILE pattern(s) detected")

        # ── Step 7: Run pytest → JUnit XML ───────────────────────────────
        update_step(pr_number, 7, "pytest executed")
        xml_path: str | None = None
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, prefix="crucible_tests_"
        ) as f:
            f.write(current_code or "")
            test_file_path = f.name

        xml_fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="crucible_results_")
        os.close(xml_fd)

        try:
            proc = await asyncio.create_subprocess_exec(
                _resolve_bin("pytest"), test_file_path, f"--junitxml={xml_path}", "-v", "--tb=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                print("[Pipeline] pytest timed out after 120s — killing process")
                stdout = b""
            print(f"[Pipeline] pytest exit={proc.returncode}")
            if proc.returncode not in (0, 1):  # 0=all pass, 1=some fail, others=error
                print(f"[Pipeline] pytest output:\n{stdout.decode()[:500]}")
        finally:
            Path(test_file_path).unlink(missing_ok=True)

        # ── Steps 8–9: TC upload + poll ────────────────────────────────────
        update_step(pr_number, 8, "Results uploaded to TC")
        tc_pass_rate: float | None = None
        try:
            if xml_path and Path(xml_path).exists():
                tc_pass_rate = await full_tc_roundtrip(xml_path)
        finally:
            # Always clean up the XML tempfile — even if full_tc_roundtrip raises
            if xml_path:
                Path(xml_path).unlink(missing_ok=True)

        update_step(pr_number, 9, "TC run polled & scored")

        # ── Confidence score ──────────────────────────────────────────────
        cats        = categorize_findings(findings)
        is_critical = has_critical(findings)
        result      = compute_confidence(
            iteration      = iteration,
            warnings       = len(cats["WARNING"]),
            errors         = len(cats["ERROR"]),
            any_critical   = is_critical,
            tc_pass_rate   = tc_pass_rate,
            fragile_count  = fragile_count,
            req_coverage   = {r["req_id"]: 1 for r in requirements},  # 1 test per req minimum
        )

        print(f"[Pipeline] Confidence={result.confidence:.2f} Gate={result.gate}")

        # Persist final state to SQLite
        upsert_run(
            pr_number,
            step             = 10,
            step_label       = "Complete",
            confidence       = result.confidence,
            tc_pass_rate     = tc_pass_rate,
            findings         = findings,
            req_coverage     = result.req_coverage,
            fragile_count    = fragile_count,
            security_summary = security_summary,
            gate             = result.gate,
        )

        # ── Maestro (CRITICAL only) ────────────────────────────────────────
        maestro_case_id: str | None = None
        if is_critical:
            maestro_case_id = await _open_maestro_case(findings, pr_number)

        # ── PR comment ────────────────────────────────────────────────────
        comment = format_pr_comment(
            result,
            pr_number       = pr_number,
            security_summary = security_summary,
            maestro_case_id = maestro_case_id,
        )
        await _post_github_comment(pr_number, comment)

        print(f"[Pipeline] PR #{pr_number} complete — {result.gate_emoji} {result.gate}")

    except Exception as e:
        print(f"[Pipeline] ERROR on PR #{pr_number}: {e}")
        traceback.print_exc()
        upsert_run(pr_number, step=-1, step_label="Error", error_message=str(e))
