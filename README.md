# Crucible

**Multi-Agent Testing Intelligence Platform**

> Three agents. One adversarial loop. Zero untested edge cases.

Crucible watches your pull requests, generates production-quality tests with a writer/critic adversarial loop, runs a security scan, and ships JUnit XML results into UiPath Test Cloud — then reads the actual pass/fail data back to ground its confidence score. Every test is traceable to a requirement. Every flaky test is flagged before it merges.

Built for **UiPath AgentHack · Track 3: Test Cloud**.

🤖 Built with [Claude Code](https://claude.ai/code)

---

## Architecture

```
GitHub Webhook → asyncio.Queue → Orchestrator
                                    ├─ Requirements Analyzer  (claude-haiku-4-5)
                                    ├─ Writer/Critic loop     (claude-sonnet-4-6 × 2)
                                    ├─ Security Agent         (semgrep + claude-sonnet-4-6)
                                    ├─ pytest runner          (JUnit XML)
                                    └─ Test Cloud client      (upload → trigger → poll)
                                           ↓
                                    Confidence Score → PR Comment
                                           ↓ (if CRITICAL)
                                    Maestro Case
```

**Confidence formula:**
```
confidence = 0.3 × critic_score + 0.3 × security_score + 0.4 × tc_pass_rate
```
The 40% weight on `tc_pass_rate` is real execution data from Test Cloud — not static analysis.

---

## Quick Start

### 1. Set up environment

```bash
cd crucible/
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Fill in ANTHROPIC_API_KEY, TC_BASE_URL, TC_TOKEN, TC_PROJECT_ID, GITHUB_*
```

### 2. Run Day 1 validation (required before anything else)

```bash
python validate_apis.py
```

All 7 checks must pass before writing any agent code:
1. Anthropic API key valid
2. TC JUnit XML upload works
3. TC GET /test-runs schema confirmed
4. TC POST /test-runs trigger works
5. TC poll schema confirmed
6. Semgrep installed and rulesets valid

### 3. Test TC client standalone (Day 2)

```bash
# Dry run (no TC API calls):
TC_DRY_RUN=true python tc_client.py

# Real run with an XML file:
python tc_client.py path/to/results.xml
```

### 4. Run unit tests

```bash
pytest tests/ -v
```

Includes:
- `tests/test_hmac.py` — 8 HMAC verification tests
- `tests/test_confidence.py` — 14 confidence formula tests (including demo math verification)

### 5. Start the full stack

```bash
# Local:
uvicorn main:app --reload &
streamlit run dashboard.py

# Docker:
docker-compose up --build
```

Webhook server: http://localhost:8000
Dashboard:      http://localhost:8501
Health check:   http://localhost:8000/health

### 6. Configure GitHub webhook

In your repo → Settings → Webhooks → Add webhook:
- Payload URL: `https://your-server/webhook`
- Content type: `application/json`
- Secret: value of `GITHUB_WEBHOOK_SECRET` in your `.env`
- Events: Pull requests

---

## Development Flags

| Flag | Default | Effect |
|---|---|---|
| `TC_DRY_RUN=true` | `false` | Stubs `tc_pass_rate=0.78`, no TC API calls |
| `GITHUB_WEBHOOK_SECRET` unset | — | HMAC verification skipped with warning |
| `MAESTRO_BASE_URL` unset | — | Maestro escalation skipped silently |

---

## File Structure

```
crucible/
├── main.py              # FastAPI webhook server (HMAC + asyncio.Queue)
├── pipeline.py          # Full orchestrator — calls all agents
├── confidence.py        # Confidence formula + PR comment formatter
├── tc_client.py         # Test Cloud client (upload → trigger → poll)
├── dashboard.py         # Streamlit live dashboard
├── validate_apis.py     # Day 1 validation checklist (run first!)
├── agents/
│   ├── requirements_analyzer.py  # haiku — extracts req_ids from PR
│   ├── writer.py                 # sonnet — generates pytest file
│   ├── critic.py                 # sonnet — adversarial review
│   └── security.py               # sonnet + semgrep — security scan
├── state/
│   └── db.py            # SQLite shared state (FastAPI writes, Streamlit reads)
├── tests/
│   ├── test_hmac.py     # HMAC unit tests
│   └── test_confidence.py  # Confidence formula unit tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Confidence Score

| Component | Weight | Source |
|---|---|---|
| `critic_score` | 30% | `max(0.5, 1.1 − 0.1×iteration)` |
| `security_score` | 30% | Semgrep findings (WARNING cap at 4, CRITICAL=0.0) |
| `tc_pass_rate` | 40% | Real Test Cloud execution result |

**Merge gates:**
- `≥ 0.85` → ✅ Auto-approve
- `0.65–0.84` → ⚠️ Review recommended
- `< 0.65` or CRITICAL → ❌ Blocked

**Fallback** (TC unavailable): `0.6×critic + 0.4×security` with "TC Pending" label.

---

## Real API Gotcha

> ⚠️ GitHub's webhook payload includes `changed_files` as an **integer** (the count), not an array. Fetching the actual file list requires a separate call to `GET /repos/{owner}/{repo}/pulls/{number}/files`. Crucible handles this explicitly — see `main.py:_fetch_pr_files()`.

---

## License

MIT
