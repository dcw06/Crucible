"""
dashboard.py — Crucible Streamlit Dashboard
============================================
Reads pipeline state from SQLite (shared with the FastAPI server).
Polls every 500ms — no pub/sub needed for a demo.

Run:
    streamlit run dashboard.py --server.port 8501
"""

import hashlib
import hmac
import json
import os
import time

import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

from agents.security import categorize_findings
from state import get_run, list_runs

WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
DEMO_PAYLOAD = {
    "action": "opened",
    "number": 1,
    "pull_request": {
        "number": 1,
        "title": "feat: add user analytics module",
        "state": "open",
        "head": {"sha": "demo", "ref": "demo/vulnerable-code"},
        "base": {"ref": "main"},
    },
    "repository": {"name": "Crucible", "owner": {"login": "dcw06"}},
}


def _trigger_demo():
    body = json.dumps(DEMO_PAYLOAD, separators=(",", ":")).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    try:
        httpx.post(
            "http://localhost:8000/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": sig,
            },
            timeout=5,
        )
    except Exception:
        pass

# Outline §10 — exactly 9 display steps matching the demo plan
# (state/db.py has a separate PIPELINE_STEPS used for step-label tracking;
#  this list is for the sidebar indicator only)
PIPELINE_STEPS = [
    "Webhook received & verified",
    "PR files fetched",
    "Requirements analyzed",
    "Tests generated (Writer)",
    "Tests reviewed (Critic)",
    "Security scanned (Semgrep)",
    "pytest executed",
    "Results uploaded to TC",
    "TC run polled & scored",
]

# ── Page config ───────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Crucible",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar: PR selector + pipeline step indicator ────────────────────────

st.sidebar.title("🔥 Crucible")
st.sidebar.caption("Multi-Agent Testing Intelligence")
st.sidebar.divider()

st.sidebar.markdown(
    "Crucible automatically generates tests, runs security scans, "
    "and gates merges — triggered by every PR."
)
if st.sidebar.button("▶ Run Demo Pipeline", use_container_width=True, type="primary"):
    _trigger_demo()
    st.sidebar.success("Pipeline triggered! Watch the steps below.")

st.sidebar.markdown(
    "[![GitHub](https://img.shields.io/badge/GitHub-dcw06%2FCrucible-black?logo=github)]"
    "(https://github.com/dcw06/Crucible)"
)
st.sidebar.divider()

# Recent PRs dropdown
recent = list_runs()
recent_labels = [f"PR #{r['pr_number']} — {r.get('gate', '…')}" for r in recent]
pr_options = [r["pr_number"] for r in recent]

if pr_options:
    selected_idx = st.sidebar.selectbox(
        "Select PR", range(len(pr_options)),
        format_func=lambda i: recent_labels[i]
    )
    pr_number = pr_options[selected_idx]
else:
    pr_number = st.sidebar.number_input("PR Number", min_value=1, value=1, step=1)

st.sidebar.divider()
st.sidebar.subheader("Pipeline State")
# Single replaceable slot — render_pipeline_steps() writes into this each tick
# so it replaces previous output instead of accumulating lines.
_steps_slot = st.sidebar.empty()

# ── Main content placeholder (updated in polling loop) ────────────────────
main_placeholder = st.empty()


def _gate_color(gate: str | None) -> str:
    return {"APPROVED": "green", "REVIEW": "orange", "BLOCKED": "red"}.get(gate or "", "gray")


def _gate_emoji(gate: str | None) -> str:
    return {"APPROVED": "✅", "REVIEW": "⚠️", "BLOCKED": "❌"}.get(gate or "", "🔄")


def render_pipeline_steps(current_step: int, step_label: str, slot=None):
    """Render the sidebar pipeline step indicator into a replaceable slot.

    Pass a `slot` created with st.sidebar.empty() so repeated calls replace
    the previous output rather than accumulating lines in the sidebar.
    """
    target = slot if slot is not None else st.sidebar.empty()
    with target.container():
        for i, name in enumerate(PIPELINE_STEPS, 1):
            if i < current_step:
                st.write(f"✅ {name}")
            elif i == current_step:
                st.write(f"🔄 **[STEP {i}/{len(PIPELINE_STEPS)}: {name}...]**")
            else:
                st.write(f"⬜ {name}")


def render_dashboard(run: dict | None):
    """Render the main dashboard content."""
    with main_placeholder.container():

        if run is None:
            st.markdown("## 👋 Welcome to Crucible")
            st.markdown(
                "Crucible is a multi-agent CI/CD pipeline that automatically "
                "generates tests, scans for vulnerabilities, and gates merges.\n\n"
                "**Click ▶ Run Demo Pipeline** in the sidebar to watch it run live."
            )
            st.info("No pipeline run yet — trigger one from the sidebar to get started.")
            with st.expander("How it works"):
                st.markdown(
                    "1. **Webhook** — GitHub PR triggers the pipeline\n"
                    "2. **Requirements** — Claude extracts what the code should do\n"
                    "3. **Writer ↔ Critic loop** — two agents adversarially generate & harden tests\n"
                    "4. **Security scan** — Semgrep scans the PR source for vulnerabilities\n"
                    "5. **pytest** — tests run locally\n"
                    "6. **Test Cloud** — results uploaded to UiPath Test Manager\n"
                    "7. **Gate decision** — APPROVED or BLOCKED with a PR comment"
                )
            return

        step       = run.get("step", 0)
        step_label = run.get("step_label", "Queued")
        gate       = run.get("gate")
        confidence = run.get("confidence")
        tc_rate    = run.get("tc_pass_rate")
        fragile    = run.get("fragile_count", 0)
        findings   = run.get("findings") or []
        req_cov    = run.get("req_coverage") or {}
        sec_summary = run.get("security_summary", "")
        error_msg  = run.get("error_message")

        # ── Error state ───────────────────────────────────────────────────
        if step == -1:
            st.error(f"❌ Pipeline error: {error_msg}")
            return

        # ── Header ────────────────────────────────────────────────────────
        col_title, col_gate = st.columns([3, 1])
        with col_title:
            st.title(f"PR #{pr_number}")
            st.caption(f"Current step: **{step_label}**")
        with col_gate:
            if gate:
                color = _gate_color(gate)
                emoji = _gate_emoji(gate)
                st.markdown(
                    f"<div style='text-align:center; padding:16px; "
                    f"border-radius:8px; background:{color}20; "
                    f"border:2px solid {color}'>"
                    f"<h2 style='color:{color};margin:0'>{emoji} {gate}</h2>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

        st.divider()

        # ── Metrics row ───────────────────────────────────────────────────
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            if confidence is not None:
                st.metric("Confidence Score", f"{confidence:.2f}")
            else:
                st.metric("Confidence Score", "—")
        with m2:
            if tc_rate is not None:
                st.metric("TC Pass Rate", f"{tc_rate:.0%}")
            else:
                st.metric("TC Pass Rate", "Pending…")
        with m3:
            # Use categorize_findings() so semgrep ERROR is correctly mapped to CRITICAL
            cats = categorize_findings(findings)
            critical_count = len(cats["CRITICAL"])
            st.metric("Security Issues", f"{critical_count} CRITICAL / {len(findings)} total")
        with m4:
            st.metric("FRAGILE Tests", fragile)

        # ── Confidence bar ────────────────────────────────────────────────
        if confidence is not None:
            pct = int(confidence * 100)
            st.progress(confidence, text=f"Confidence: {pct}%")
        else:
            st.progress(0, text="Confidence: computing…")

        st.divider()

        # ── Two-column detail ─────────────────────────────────────────────
        left, right = st.columns(2)

        with left:
            st.subheader("🛡️ Security Findings")
            if findings:
                rows = []
                for f in findings:
                    # Apply ERROR→CRITICAL mapping so the table matches the metric above
                    raw_sev = str(f.get("extra", {}).get("severity", "INFO")).upper()
                    sev     = "CRITICAL" if raw_sev == "ERROR" else raw_sev
                    rule_id = f.get("check_id") or f.get("rule_id", "unknown")
                    line    = f.get("start", {}).get("line", "?")
                    rows.append({"Severity": sev, "Rule": rule_id, "Line": line})
                st.dataframe(rows, use_container_width=True)
            else:
                st.success("✅ No security issues found.")

            if sec_summary and sec_summary != "No security issues found.":
                with st.expander("Security summary"):
                    st.code(sec_summary, language=None)

        with right:
            st.subheader("📋 Requirements Coverage")
            if req_cov:
                rows = [
                    {"Req ID": req_id, "Tests": count,
                     "TC": "—" if tc_rate is None else ("✅" if tc_rate > 0.5 else "❌")}
                    for req_id, count in sorted(req_cov.items())
                ]
                st.dataframe(rows, use_container_width=True)
            else:
                st.info("Awaiting requirements analysis…")

        # ── TC status ─────────────────────────────────────────────────────
        if tc_rate is None and step < 9:
            st.info("🔄 Waiting for Test Cloud results…")
        elif tc_rate is not None:
            tc_pct = int(tc_rate * 100)
            tc_color = "green" if tc_pct >= 80 else ("orange" if tc_pct >= 50 else "red")
            st.markdown(
                f"**Test Cloud:** `{tc_pct}% pass rate`  "
                f"<span style='color:{tc_color}'>{'●' * (tc_pct // 10)}{'○' * (10 - tc_pct // 10)}</span>",
                unsafe_allow_html=True,
            )


# ── Polling loop ──────────────────────────────────────────────────────────

while True:
    run = get_run(pr_number)

    # Update sidebar step indicator
    current_step  = (run or {}).get("step", 0)
    current_label = (run or {}).get("step_label", "Queued")
    # Write into the pre-allocated slot — replaces previous content each tick
    render_pipeline_steps(current_step, current_label, _steps_slot)

    # Render main content
    render_dashboard(run)

    # Stop polling when complete or errored
    if run and run.get("step") in (10, -1):
        break

    time.sleep(0.5)
