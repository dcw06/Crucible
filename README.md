# Crucible — original PR testing prototype

[![Core tests](https://github.com/dcw06/Crucible/actions/workflows/ci.yml/badge.svg)](https://github.com/dcw06/Crucible/actions/workflows/ci.yml)

The original implementation of a pull-request testing workflow: extract requirements, generate tests, critique them, scan changed source, run tests, and report execution results through UiPath Test Cloud.

**[Expanded implementation](https://github.com/dcw06/Crucible-Updated-7-30-) · [Setup and architecture](docs/OPERATIONS.md) · [Core tests](https://github.com/dcw06/Crucible/actions)**

This repository remains public as a historical prototype. The expanded repository adds durable jobs, stricter evidence gates, upload scanning, and separate attested workers. Use that implementation when evaluating the later security architecture.

## Scope

FastAPI webhook → in-memory queue → requirements analyzer → Writer/Critic loop → security scan → execution/Test Cloud → confidence score and PR feedback.

The confidence score is a weighted policy heuristic, not a calibrated correctness probability. This implementation does not establish complete test coverage, universal flake detection, or security certification. Its in-memory queue is not restart-durable.

## Verify core behavior

```bash
git clone https://github.com/dcw06/Crucible.git
cd Crucible
python3 -m venv .venv
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest tests -q
```

The core suite needs no API keys or model calls. For the full integrations, use the [operations guide](docs/OPERATIONS.md). Webhook signature verification rejects requests when `GITHUB_WEBHOOK_SECRET` is missing, and server startup requires it.

Maintained by Chongwei (Alan) Dai. Claude Code assistance and commit coauthors remain attributed. [MIT license](LICENSE).
