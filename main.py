"""
main.py — Crucible FastAPI Webhook Server
==========================================
Receives GitHub PR webhooks, verifies HMAC, and queues the pipeline.

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

HMAC note: hmac.new() takes positional args to avoid Python version differences:
    hmac.new(key, msg, digestmod)  — NOT hmac.new(key, msg=..., digestmod=...)

Unit tests for HMAC are in tests/test_hmac.py — run these before Day 5 integration.
"""

import asyncio
import json
import os

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

load_dotenv(dotenv_path=".env", override=True)

from auth import verify_signature
from pipeline import run_pipeline
from state import init_db

GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_OWNER   = os.environ.get("GITHUB_OWNER", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")

app = FastAPI(title="Crucible Webhook Server")

# Single asyncio.Queue — one background worker processes PRs sequentially.
# This ensures the event loop is never blocked by subprocess/HTTP calls in the pipeline.
pipeline_queue: asyncio.Queue = asyncio.Queue()


# ── Background worker ─────────────────────────────────────────────────────

async def pipeline_worker() -> None:
    """
    Processes pipeline jobs from the queue one at a time.
    Never touches the event loop directly — all I/O uses async primitives.
    """
    print("[Worker] Pipeline worker started")
    while True:
        item = await pipeline_queue.get()
        try:
            await run_pipeline(**item)
        except Exception as e:
            print(f"[Worker] Unhandled exception: {e}")
        finally:
            pipeline_queue.task_done()


# ── Startup / shutdown ────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    init_db()
    asyncio.create_task(pipeline_worker())
    print("[Crucible] Server started — pipeline worker running")


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "queue_size": pipeline_queue.qsize()}


@app.post("/webhook")
async def github_webhook(request: Request) -> JSONResponse:
    """
    Receive GitHub PR webhook, verify HMAC, fetch file list, enqueue pipeline.

    Note: payload["pull_request"]["changed_files"] is an INTEGER (the count).
    The actual file list requires a separate call to GET /repos/.../pulls/{n}/files.
    """
    body = await request.body()
    sig  = request.headers.get("X-Hub-Signature-256", "")

    if not verify_signature(body, sig):
        raise HTTPException(status_code=403, detail="Invalid HMAC signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event  = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    # Only process PR open/update events
    if event != "pull_request" or action not in ("opened", "synchronize"):
        return JSONResponse({"status": "ignored", "reason": f"event={event} action={action}"})

    pr = payload.get("pull_request", {})
    pr_number = pr.get("number")
    if not pr_number:
        raise HTTPException(status_code=400, detail="Missing pull_request.number")

    # changed_files is an integer — must fetch file list from API
    # (This is a real GitHub API gotcha: the payload field is a count, not an array)
    files = await _fetch_pr_files(pr_number)
    if files is None:
        raise HTTPException(status_code=502, detail="Failed to fetch PR files from GitHub")

    print(f"[Webhook] ✅ PR #{pr_number} queued ({len(files)} files changed)")
    await pipeline_queue.put({"files": files, "pr_number": pr_number})

    return JSONResponse({"status": "accepted", "pr_number": pr_number, "files": len(files)})


async def _fetch_pr_files(pr_number: int) -> list[dict] | None:
    """
    Fetch the list of changed files for a PR.
    Returns list of {filename, patch, status} dicts, or None on error.
    """
    missing = [v for v, val in [
        ("GITHUB_TOKEN", GITHUB_TOKEN),
        ("GITHUB_OWNER", GITHUB_OWNER),
        ("GITHUB_REPO",  GITHUB_REPO),
    ] if not val]
    if missing:
        print(f"[Webhook] {', '.join(missing)} not set — returning empty file list")
        return []

    url = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
           f"/pulls/{pr_number}/files")
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )

    if resp.status_code != 200:
        print(f"[Webhook] Failed to fetch PR files: HTTP {resp.status_code}")
        return None

    return resp.json()  # array of {filename, patch, status, ...}
