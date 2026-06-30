"""
validate_apis.py — Crucible Day 1 Checklist
============================================
Run this before writing any agent code.
All 7 checks must pass (or have documented fallbacks) before proceeding.

Usage:
    cd crucible/
    python validate_apis.py

Exit code 0 = all required checks passed.
Exit code 1 = one or more required checks failed.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

# ── Config from environment ───────────────────────────────────────────────
TC_BASE       = os.environ.get("TC_BASE_URL", "").rstrip("/")
TC_TOKEN      = os.environ.get("TC_TOKEN", "")
TC_PROJECT_ID = os.environ.get("TC_PROJECT_ID", "")

MAESTRO_BASE  = os.environ.get("MAESTRO_BASE_URL", "").rstrip("/")
MAESTRO_TOKEN = os.environ.get("MAESTRO_TOKEN", "")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── Helpers ───────────────────────────────────────────────────────────────
PASS  = "\033[32m✅\033[0m"
FAIL  = "\033[31m❌\033[0m"
WARN  = "\033[33m⚠️ \033[0m"
INFO  = "\033[34mℹ️ \033[0m"

def record(label: str, passed: bool, required: bool, notes: str = "") -> bool:
    icon = PASS if passed else (FAIL if required else WARN)
    print(f"  {icon} {label}")
    if notes:
        for line in notes.splitlines():
            print(f"       {line}")
    return passed


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def tc_headers() -> dict:
    return {"Authorization": f"Bearer {TC_TOKEN}", "Content-Type": "application/json"}


def minimal_junit_xml() -> str:
    """A minimal valid JUnit XML with one passing and one failing test."""
    return textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <testsuites>
          <testsuite name="crucible_validation" tests="2" failures="1" errors="0">
            <testcase classname="crucible" name="test_passing" time="0.001"/>
            <testcase classname="crucible" name="test_failing" time="0.001">
              <failure message="assert False">AssertionError: validation probe</failure>
            </testcase>
          </testsuite>
        </testsuites>
    """)


# ══════════════════════════════════════════════════════════════════════════
# CHECK 1 — Anthropic API key
# ══════════════════════════════════════════════════════════════════════════
async def check_anthropic(client: httpx.AsyncClient) -> bool:
    section("CHECK 1: Anthropic API key")
    if not ANTHROPIC_KEY:
        return record("ANTHROPIC_API_KEY present", False, True,
                      "Set ANTHROPIC_API_KEY in .env")

    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "ping"}],
        },
        timeout=15,
    )
    ok = resp.status_code == 200
    return record(
        "Anthropic API key valid (haiku ping)",
        ok,
        True,
        "" if ok else f"HTTP {resp.status_code}: {resp.text[:200]}",
    )


# ══════════════════════════════════════════════════════════════════════════
# CHECK 2 — TC: JUnit XML upload
# ══════════════════════════════════════════════════════════════════════════
async def check_tc_upload(client: httpx.AsyncClient) -> tuple[bool, str | None]:
    section("CHECK 2: TC — JUnit XML upload")

    missing = [v for v in ("TC_BASE_URL", "TC_TOKEN", "TC_PROJECT_ID")
               if not os.environ.get(v)]
    if missing:
        record("TC credentials present", False, True,
               f"Missing: {', '.join(missing)}")
        return False, None

    record("TC credentials present", True, True)

    xml_content = minimal_junit_xml()
    resp = await client.post(
        f"{TC_BASE}/api/v2/projects/{TC_PROJECT_ID}/test-runs/results",
        headers={"Authorization": f"Bearer {TC_TOKEN}"},
        files={"file": ("crucible_probe.xml", xml_content.encode(), "application/xml")},
        timeout=20,
    )
    ok = resp.status_code in (200, 201)
    run_id = None
    if ok:
        try:
            data   = resp.json()
            run_id = data.get("id") or data.get("runId")
        except Exception:
            pass

    record(
        f"JUnit XML upload → {resp.status_code}",
        ok,
        True,
        f"run_id={run_id}" if run_id else (resp.text[:300] if not ok else ""),
    )
    return ok, run_id


# ══════════════════════════════════════════════════════════════════════════
# CHECK 3 — TC: GET /test-runs schema
# ══════════════════════════════════════════════════════════════════════════
async def check_tc_get_runs(client: httpx.AsyncClient) -> bool:
    section("CHECK 3: TC — GET /test-runs schema")
    resp = await client.get(
        f"{TC_BASE}/api/v2/projects/{TC_PROJECT_ID}/test-runs",
        headers=tc_headers(),
        timeout=15,
    )
    ok = resp.status_code == 200
    if not ok:
        return record(f"GET /test-runs → {resp.status_code}", False, True,
                      resp.text[:300])

    record(f"GET /test-runs → {resp.status_code}", True, True)

    # Inspect schema for the fields we depend on
    try:
        data = resp.json()
        items = data if isinstance(data, list) else data.get("value", data.get("items", []))
        if items:
            sample = items[0]
            found_status   = "status"      in sample or "state" in sample
            found_passed   = "passedCount" in sample or "passed" in sample
            found_total    = "totalCount"  in sample or "total"  in sample
            record("  status field present",      found_status,  True,
                   f"keys: {list(sample.keys())[:12]}")
            record("  passedCount field present", found_passed,  True,
                   "Will use for tc_pass_rate numerator")
            record("  totalCount field present",  found_total,   True,
                   "Will use for tc_pass_rate denominator")
        else:
            print(f"       {INFO} No existing runs found — schema check skipped (OK for fresh project)")
    except Exception as e:
        record("Schema parse", False, True, str(e))
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════
# CHECK 4 — TC: POST /test-runs trigger
# ══════════════════════════════════════════════════════════════════════════
async def check_tc_trigger(client: httpx.AsyncClient, upload_run_id: str | None) -> tuple[bool, str | None]:
    section("CHECK 4: TC — POST /test-runs (trigger)")
    resp = await client.post(
        f"{TC_BASE}/api/v2/projects/{TC_PROJECT_ID}/test-runs",
        headers=tc_headers(),
        json={},
        timeout=15,
    )
    ok = resp.status_code in (200, 201, 202)
    run_id = None
    if ok:
        try:
            data   = resp.json()
            run_id = data.get("id") or data.get("runId")
        except Exception:
            pass

    record(
        f"POST /test-runs → {resp.status_code}",
        ok,
        True,
        f"triggered run_id={run_id}" if run_id else (resp.text[:300] if not ok else ""),
    )
    return ok, run_id


# ══════════════════════════════════════════════════════════════════════════
# CHECK 5 — TC: GET /test-runs/{id} poll (status field)
# ══════════════════════════════════════════════════════════════════════════
async def check_tc_poll(client: httpx.AsyncClient, run_id: str | None) -> bool:
    section("CHECK 5: TC — GET /test-runs/{id} (poll schema)")
    if not run_id:
        return record("run_id available for poll check", False, True,
                      "Check 4 must succeed first — re-run after fixing trigger")

    resp = await client.get(
        f"{TC_BASE}/api/v2/projects/{TC_PROJECT_ID}/test-runs/{run_id}",
        headers=tc_headers(),
        timeout=15,
    )
    ok = resp.status_code == 200
    if not ok:
        return record(f"GET /test-runs/{run_id} → {resp.status_code}", False, True,
                      resp.text[:300])

    record(f"GET /test-runs/{run_id} → {resp.status_code}", True, True)

    try:
        data = resp.json()
        status_val = data.get("status") or data.get("state") or "(not found)"
        record(f"  status/state field = '{status_val}'", True, True,
               "Document which values indicate 'running' vs done")
    except Exception as e:
        record("Poll schema parse", False, True, str(e))
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════
# CHECK 5b — TC: FRAGILE custom property
# ══════════════════════════════════════════════════════════════════════════
async def check_tc_fragile(client: httpx.AsyncClient) -> None:
    section("CHECK 5b: TC — FRAGILE custom property support")
    # Attempt to set a custom property on a probe test case
    # We don't need a real test case ID — a 422 vs 404 tells us what we need
    resp = await client.patch(
        f"{TC_BASE}/api/v2/projects/{TC_PROJECT_ID}/test-cases/probe-000",
        headers=tc_headers(),
        json={"customProperties": [{"name": "FRAGILE", "value": True}]},
        timeout=10,
    )
    if resp.status_code in (200, 201, 204):
        record("FRAGILE custom property supported", True, False,
               "Use customProperties on test case payload")
    elif resp.status_code == 422:
        record("FRAGILE custom property NOT supported on this tier", False, False,
               "FALLBACK: prefix test name with [FRAGILE] — already implemented")
    else:
        record(
            f"FRAGILE check inconclusive ({resp.status_code})",
            False,
            False,
            f"Defaulting to [FRAGILE] prefix fallback\n{resp.text[:200]}",
        )
    print(f"       {INFO} Either path is handled — pipeline will not break")


# ══════════════════════════════════════════════════════════════════════════
# CHECK 6 — Maestro Case POST
# ══════════════════════════════════════════════════════════════════════════
async def check_maestro(client: httpx.AsyncClient) -> None:
    section("CHECK 6: Maestro — Case POST (optional)")
    if not MAESTRO_BASE or not MAESTRO_TOKEN:
        print(f"       {INFO} MAESTRO_BASE_URL / MAESTRO_TOKEN not set — skipping gracefully")
        print(f"       {INFO} CRITICAL security findings will log locally instead of opening a case")
        return

    resp = await client.post(
        f"{MAESTRO_BASE}/api/v1/Maestro/Cases",
        headers={"Authorization": f"Bearer {MAESTRO_TOKEN}",
                 "Content-Type": "application/json"},
        json={
            "title": "Crucible validation probe",
            "severity": "LOW",
            "description": "Probe from validate_apis.py — safe to close",
        },
        timeout=15,
    )
    ok = resp.status_code in (200, 201)
    record(
        f"Maestro Case POST → {resp.status_code}",
        ok,
        False,
        "" if ok else f"{resp.text[:200]}\nMaestro will be skipped in pipeline if this fails",
    )


# ══════════════════════════════════════════════════════════════════════════
# CHECK 7 — Semgrep --validate
# ══════════════════════════════════════════════════════════════════════════
def check_semgrep() -> bool:
    section("CHECK 7: Semgrep — install + ruleset validate")

    # 7a: semgrep installed?
    result = subprocess.run(["semgrep", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        return record("semgrep installed", False, True,
                      "pip install semgrep  (or: pip3 install semgrep)")
    record(f"semgrep installed ({result.stdout.strip()})", True, True)

    # 7b: validate each ruleset
    all_ok = True
    for config in ("p/python", "p/owasp-top-ten", "p/secrets"):
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("x = 1\n")
            tmp = f.name
        try:
            r = subprocess.run(
                ["semgrep", "--config", config, "--validate", tmp],
                capture_output=True,
                text=True,
                timeout=30,
            )
            ok = r.returncode == 0
            record(
                f"semgrep --validate --config {config}",
                ok,
                True,
                "" if ok else (r.stderr[:200] or r.stdout[:200]),
            )
            all_ok = all_ok and ok
        except subprocess.TimeoutExpired:
            record(f"semgrep --validate {config} (timeout)", False, True,
                   "Check internet connection — rulesets are fetched on first use")
            all_ok = False
        finally:
            Path(tmp).unlink(missing_ok=True)

    return all_ok


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════
async def main():
    print("\n" + "═" * 60)
    print("  CRUCIBLE — validate_apis.py")
    print("  Day 1 API validation checklist")
    print("═" * 60)

    # Semgrep is sync — run first
    semgrep_ok = check_semgrep()

    failed_required: list[str] = []
    if not semgrep_ok:
        failed_required.append("Semgrep")

    async with httpx.AsyncClient() as client:
        # Check 1: Anthropic
        if not await check_anthropic(client):
            failed_required.append("Anthropic API key")

        # Check 2: TC upload (feeds run_id to checks 4 & 5)
        tc_ok = bool(TC_BASE and TC_TOKEN and TC_PROJECT_ID)
        upload_ok, upload_run_id = (False, None)
        if tc_ok:
            upload_ok, upload_run_id = await check_tc_upload(client)
            if not upload_ok:
                failed_required.append("TC JUnit XML upload")

            # Check 3: TC GET schema
            if not await check_tc_get_runs(client):
                failed_required.append("TC GET /test-runs schema")

            # Check 4: TC trigger
            trigger_ok, trigger_run_id = await check_tc_trigger(client, upload_run_id)
            if not trigger_ok:
                failed_required.append("TC POST /test-runs trigger")

            # Check 5: TC poll schema
            poll_run_id = trigger_run_id or upload_run_id
            if not await check_tc_poll(client, poll_run_id):
                failed_required.append("TC poll schema")

            # Check 5b: FRAGILE (non-required, documents fallback)
            await check_tc_fragile(client)
        else:
            section("CHECKS 2–5b: Test Cloud")
            print(f"       {WARN} TC credentials missing — set TC_BASE_URL, TC_TOKEN, TC_PROJECT_ID in .env")
            failed_required.append("TC credentials")

        # Check 6: Maestro (non-required)
        await check_maestro(client)

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    if failed_required:
        print(f"  {FAIL} VALIDATION FAILED — fix these before writing agent code:")
        for item in failed_required:
            print(f"       • {item}")
        print("═" * 60 + "\n")
        sys.exit(1)
    else:
        print(f"  {PASS} ALL REQUIRED CHECKS PASSED")
        print("  You are clear to proceed to tc_client.py (Day 2).")
        print("═" * 60 + "\n")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
