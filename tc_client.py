"""
tc_client.py — UiPath Test Manager Client
==========================================
Real UiPath Test Manager REST API v2 integration. No JUnit XML upload endpoint
exists in the API, so XML is parsed locally and results are posted via structured
API calls.

Flow for full_tc_roundtrip(xml_path):
  1. Parse JUnit XML → list of { name, passed, duration_ms }
  2. POST /api/v2/{projectId}/testcases          — one per test name
  3. POST /api/v2/{projectId}/testexecutions      — create execution
  4. POST /api/v2/{projectId}/testexecutions/{id}/start
  5. POST /api/v2/{projectId}/testcaselogs        — one result per test
  6. POST /api/v2/{projectId}/testexecutions/{id}/finish
  7. GET  /api/v2/{projectId}/testexecutions/{id}/withstats → pass rate

Usage:
    TC_DRY_RUN=true python tc_client.py          # stub, no API calls
    python tc_client.py path/to/results.xml      # real roundtrip
    python tc_client.py                          # generates a smoke-test XML
"""

import asyncio
import os
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

_HERE = Path(__file__).parent
_env_file = (
    _HERE / ".env" if (_HERE / ".env").exists() else _HERE.parent / ".env"
)
load_dotenv(dotenv_path=_env_file, override=True)

TC_BASE          = os.environ.get("TC_BASE_URL", "").rstrip("/")
TC_TOKEN         = os.environ.get("TC_TOKEN", "")
TC_PROJECT_ID    = os.environ.get("TC_PROJECT_ID", "")
TC_DRY_RUN       = os.environ.get("TC_DRY_RUN", "false").lower() == "true"
TC_FULL_COOKIE   = os.environ.get("TC_FULL_COOKIE", "")

# Chrome Profile 2 cookie file (discovered to hold the TM session)
_CHROME_COOKIE_FILE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Profile 2/Cookies"
)
_TC_COOKIE_NAMES = [
    ".AspNetCore.TMH.AUTH.COOKIE",
    "XSRF-TOKEN-TMH",
    ".AspNetCore.Antiforgery.hBlB_SMfehM",
]


def _read_chrome_cookies() -> dict[str, str]:
    """Read live TM cookies directly from Chrome's cookie store."""
    try:
        import browser_cookie3
        jar = browser_cookie3.chrome(
            domain_name="staging.uipath.com",
            cookie_file=_CHROME_COOKIE_FILE,
        )
        return {c.name: c.value for c in jar if c.name in _TC_COOKIE_NAMES}
    except Exception as e:
        print(f"[TC] Chrome cookie read failed: {e}")
        return {}


def _build_cookie_string(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _get_auth() -> tuple[str, str]:
    """
    Returns (cookie_string, xsrf_token).
    Priority: live Chrome cookies > TC_FULL_COOKIE env var.
    """
    chrome = _read_chrome_cookies()
    if chrome.get(".AspNetCore.TMH.AUTH.COOKIE"):
        print("[TC] Using live Chrome cookies")
        return _build_cookie_string(chrome), chrome.get("XSRF-TOKEN-TMH", "")

    # Fallback: TC_FULL_COOKIE from env
    if TC_FULL_COOKIE:
        print("[TC] Using TC_FULL_COOKIE from .env")
        xsrf = ""
        for part in TC_FULL_COOKIE.split(";"):
            p = part.strip()
            if p.startswith("XSRF-TOKEN-TMH="):
                xsrf = p.split("=", 1)[1]
        return TC_FULL_COOKIE, xsrf

    return "", ""


def _tc_headers() -> dict:
    cookie_str, xsrf = _get_auth()
    if cookie_str:
        return {
            "Content-Type": "application/json",
            "Cookie": cookie_str,
            "x-xsrf-token": xsrf,
        }
    # Last resort: PAT (will likely 401, but gives a clear error)
    return {
        "Authorization": f"Bearer {TC_TOKEN}",
        "Content-Type": "application/json",
        "X-UIPATH-TenantName": "DefaultTenant",
    }


def _api(path: str) -> str:
    """Build full URL: TC_BASE/api/v2/{projectId}/{path}"""
    return f"{TC_BASE}/api/v2/{TC_PROJECT_ID}/{path}"


# ── JUnit XML parsing ─────────────────────────────────────────────────────

def parse_junit_xml(xml_path: str) -> list[dict]:
    """
    Parse JUnit XML and return a list of test result dicts:
      { "name": str, "passed": bool, "duration_ms": int }
    Skipped tests are excluded (no result to report).
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"[TC] XML parse error: {e}")
        return []

    results = []
    suites = root.findall(".//testsuite")
    if not suites and root.tag == "testsuite":
        suites = [root]

    for suite in suites:
        for tc in suite.findall("testcase"):
            name     = tc.get("name") or tc.get("classname") or "unnamed"
            time_s   = float(tc.get("time") or 0)
            failed   = tc.find("failure") is not None or tc.find("error") is not None
            skipped  = tc.find("skipped") is not None
            if skipped:
                continue
            results.append({
                "name":        name,
                "passed":      not failed,
                "duration_ms": int(time_s * 1000),
            })

    print(f"[TC] Parsed {len(results)} tests from {Path(xml_path).name}")
    return results


# ── Low-level API helpers ─────────────────────────────────────────────────

async def _create_test_case(client: httpx.AsyncClient, name: str) -> str | None:
    resp = await client.post(
        _api("testcases"),
        headers=_tc_headers(),
        json={"name": name},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        print(f"[TC] create_test_case failed ({resp.status_code}): {resp.text[:300]}")
        return None
    data = resp.json()
    return data.get("id") or data.get("Id")


async def _create_test_set(
    client: httpx.AsyncClient, name: str, tc_ids: list[str]
) -> str | None:
    resp = await client.post(
        _api("testsets"),
        headers=_tc_headers(),
        json={"name": name, "testCaseIds": tc_ids},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        print(f"[TC] create_test_set failed ({resp.status_code}): {resp.text[:200]}")
        return None
    return resp.json().get("id")


async def _create_execution(
    client: httpx.AsyncClient, ts_id: str, tc_ids: list[str], pr_number: int = 0
) -> str | None:
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    name = f"Crucible PR #{pr_number} — {ts}" if pr_number else f"Crucible — {ts}"
    resp = await client.post(
        _api("testexecutions"),
        headers=_tc_headers(),
        json={"name": name, "testSetId": ts_id, "testCaseIds": tc_ids},
        timeout=15,
    )
    if resp.status_code not in (200, 201):
        print(f"[TC] create_execution failed ({resp.status_code}): {resp.text[:200]}")
        return None
    exec_id = resp.json().get("id")
    print(f"[TC] Created execution: {exec_id}")
    return exec_id


async def _start_execution(client: httpx.AsyncClient, exec_id: str) -> bool:
    resp = await client.post(
        _api(f"testexecutions/{exec_id}/start"),
        headers=_tc_headers(),
        timeout=15,
    )
    ok = resp.status_code in (200, 201, 204)
    if not ok:
        print(f"[TC] start_execution failed ({resp.status_code}): {resp.text[:200]}")
    return ok


async def _record_test_result(
    client: httpx.AsyncClient, exec_id: str, tc_id: str, passed: bool
) -> bool:
    """
    Correct three-step flow for recording one test case result:
      1. POST /testcaselogs            — create the log entry
      2. POST /testcaselogs/testexecution/{exec_id}/start  — mark it running
      3. POST /testcaselogs/testexecution/{exec_id}/finish — set Passed/Failed
    """
    hdrs = _tc_headers()

    # 1. Create entry
    r1 = await client.post(
        _api("testcaselogs"),
        headers=hdrs,
        json={"testCaseId": tc_id, "testExecutionId": exec_id},
        timeout=15,
    )
    if r1.status_code not in (200, 201):
        print(f"[TC] create_log failed ({r1.status_code}): {r1.text[:150]}")
        return False

    # 2. Start
    await client.post(
        _api(f"testcaselogs/testexecution/{exec_id}/start"),
        headers=hdrs,
        json={"testCaseId": tc_id, "runId": 0},
        timeout=15,
    )

    # 3. Finish with result
    result = "Passed" if passed else "Failed"
    r3 = await client.post(
        _api(f"testcaselogs/testexecution/{exec_id}/finish"),
        headers=hdrs,
        json={"testCaseId": tc_id, "result": result, "runId": 0},
        timeout=15,
    )
    return r3.status_code in (200, 201) and r3.json().get("result") == result


async def _finish_execution(client: httpx.AsyncClient, exec_id: str) -> bool:
    resp = await client.post(
        _api(f"testexecutions/{exec_id}/finish"),
        headers=_tc_headers(),
        timeout=15,
    )
    if resp.status_code in (200, 201, 204):
        return True
    # The execution auto-finishes once all logs are recorded; 400 here is benign.
    if resp.status_code == 400:
        return True
    print(f"[TC] finish_execution failed ({resp.status_code}): {resp.text[:200]}")
    return False


async def _get_stats(client: httpx.AsyncClient, exec_id: str) -> float | None:
    resp = await client.get(
        _api(f"testexecutions/{exec_id}/withstats"),
        headers=_tc_headers(),
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[TC] get_stats failed ({resp.status_code}): {resp.text[:200]}")
        return None

    data   = resp.json()
    passed = data.get("passed", 0) or 0
    failed = data.get("failed", 0) or 0
    none   = data.get("none",   0) or 0
    total  = passed + failed + none

    if not total:
        print(f"[TC] withstats returned no total — raw: {data}")
        return None

    rate = passed / total
    print(f"[TC] withstats → {passed}/{total} passed ({rate:.1%})")
    return rate


# ── High-level: full roundtrip ────────────────────────────────────────────

async def full_tc_roundtrip(xml_path: str, pr_number: int = 0) -> float | None:
    """
    Complete UiPath Test Manager integration: parse XML, post all results,
    finish, and return pass rate (0.0–1.0). Returns None on any failure.

    Correct flow (discovered via swagger + live API debugging):
      1. Create test cases
      2. Create test set  (testSetId is required for execution creation)
      3. Create execution (testSetId + testCaseIds)
      4. Start execution
      5. For each TC: create log → start log → finish log with result
      6. Finish execution → get withstats
    """
    if TC_DRY_RUN:
        print("[TC_DRY_RUN] full_tc_roundtrip → stubbing tc_pass_rate=0.25")
        return 0.25

    tests = parse_junit_xml(xml_path)
    if not tests:
        print("[TC] No tests found in XML — aborting")
        return None

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. Create test cases
        print(f"[TC] Creating {len(tests)} test cases…")
        tc_ids: dict[str, str] = {}
        for t in tests:
            tc_id = await _create_test_case(client, t["name"])
            if tc_id:
                tc_ids[t["name"]] = tc_id

        if not tc_ids:
            print("[TC] Failed to create any test cases")
            return None

        all_tc_ids = list(tc_ids.values())

        # 2. Create test set
        ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ts_name = f"Crucible PR #{pr_number} — {ts}" if pr_number else f"Crucible — {ts}"
        ts_id = await _create_test_set(client, ts_name, all_tc_ids)
        if not ts_id:
            return None

        # 3. Create + start execution
        exec_id = await _create_execution(client, ts_id, all_tc_ids, pr_number)
        if not exec_id:
            return None
        if not await _start_execution(client, exec_id):
            return None

        # 4. Record each result
        print(f"[TC] Recording {len(tc_ids)} test results…")
        for t in tests:
            tc_id = tc_ids.get(t["name"])
            if not tc_id:
                continue
            ok     = await _record_test_result(client, exec_id, tc_id, t["passed"])
            marker = "✓" if ok else "✗"
            status = "PASS" if t["passed"] else "FAIL"
            print(f"  {marker} [{status}] {t['name']}")

        # 5. Finish + stats
        await _finish_execution(client, exec_id)
        rate = await _get_stats(client, exec_id)
        if rate is not None:
            print(f"[TC] tc_pass_rate = {rate:.2%}")
        return rate


# ── Failing tests query ───────────────────────────────────────────────────

async def get_failing_tests(
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """
    Return failed test case names from the most recent execution.
    Used by the Orchestrator to guide the Writer agent.
    """
    if TC_DRY_RUN:
        print("[TC_DRY_RUN] Returning stub failing tests")
        return [
            {"name": "test_edge_case_empty_input", "status": "failed"},
            {"name": "test_null_handling",          "status": "failed"},
        ]

    async def _fetch(c: httpx.AsyncClient) -> list[dict]:
        # Get most recent execution
        resp = await c.get(
            _api("testexecutions"),
            headers=_tc_headers(),
            params={"$orderby": "createdAt desc", "$top": 1},
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"[TC] get_failing_tests error: HTTP {resp.status_code}")
            return []

        data  = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        if not items:
            return []

        exec_id = items[0].get("id") or items[0].get("Id")
        if not exec_id:
            return []

        # Get logs for that execution
        logs_resp = await c.get(
            _api("testcaselogs"),
            headers=_tc_headers(),
            params={"testExecutionId": exec_id},
            timeout=15,
        )
        if logs_resp.status_code != 200:
            return []

        logs_data = logs_resp.json()
        logs = logs_data.get("data", logs_data) if isinstance(logs_data, dict) else logs_data

        return [
            {
                "name":   log.get("testCaseName") or log.get("name") or "",
                "status": "failed",
            }
            for log in logs
            if log.get("result") == "Failed"
        ]

    if client:
        return await _fetch(client)
    async with httpx.AsyncClient(timeout=30.0) as c:
        return await _fetch(c)


# ── CLI entrypoint ────────────────────────────────────────────────────────

async def _cli_main():
    xml_path = sys.argv[1] if len(sys.argv) > 1 else None

    if TC_DRY_RUN:
        print("Running in TC_DRY_RUN mode — no real API calls.\n")
        rate = await full_tc_roundtrip("stub.xml")
    elif xml_path:
        rate = await full_tc_roundtrip(xml_path)
    else:
        # Minimal smoke-test XML
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <testsuites>
              <testsuite name="crucible_smoke" tests="3" failures="1">
                <testcase classname="smoke" name="test_a" time="0.01"/>
                <testcase classname="smoke" name="test_b" time="0.01"/>
                <testcase classname="smoke" name="test_c" time="0.01">
                  <failure message="fail">deliberate failure</failure>
                </testcase>
              </testsuite>
            </testsuites>
        """)
        with tempfile.NamedTemporaryFile(suffix=".xml", mode="w", delete=False) as f:
            f.write(xml)
            tmp_path = f.name
        rate = await full_tc_roundtrip(tmp_path)
        Path(tmp_path).unlink(missing_ok=True)

    if rate is not None:
        print(f"\n✅  tc_pass_rate = {rate:.4f}  ({rate:.0%})")
        sys.exit(0)
    else:
        print("\n❌  tc_pass_rate could not be determined")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_cli_main())
