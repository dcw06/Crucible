"""
agents/requirements_analyzer.py — Requirements Analyzer Agent
==============================================================
Model: claude-haiku-4-5-20251001 (fast, cheap, structured JSON output)

Extracts requirement IDs from PR descriptions, docstrings, and ticket text.
Every downstream test gets a req_id tag — provides full traceability.
"""

import json
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=True)

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are a requirements analyst. Given a pull request description,
code docstrings, or ticket text, extract all testable requirements.

Return a JSON array — nothing else, no explanation, no markdown fences.

Each element must have exactly these fields:
  {
    "req_id":      string,   // e.g. "REQ-001" or "TICKET-42" or infer a short slug
    "description": string,   // one sentence describing what must be true
    "priority":    string    // "HIGH", "MEDIUM", or "LOW"
  }

Rules:
- If you find explicit IDs (e.g. JIRA tickets, REQ-NNN references), use them.
- If no explicit IDs exist, infer short slugs from the requirement (e.g. "REQ-NULL-SAFETY").
- If no requirements can be extracted at all, return an empty array [].
- Never return anything other than a valid JSON array."""


def _call_claude(client: anthropic.Anthropic, user_message: str) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    block = response.content[0]
    return block.text.strip() if block.type == "text" else ""


def _parse_requirements(raw: str) -> list[dict]:
    """Parse JSON from model output, stripping accidental fences."""
    # Strip markdown fences if model misbehaves
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        valid = []
        for item in data:
            if isinstance(item, dict) and "req_id" in item and "description" in item:
                valid.append({
                    "req_id":      str(item["req_id"]).strip(),
                    "description": str(item["description"]).strip(),
                    "priority":    str(item.get("priority", "MEDIUM")).upper(),
                })
        return valid
    except json.JSONDecodeError:
        return []


def analyze_requirements(
    pr_description: str = "",
    docstrings: str = "",
    ticket_text: str = "",
) -> list[dict]:
    """
    Extract requirements from PR metadata.

    Returns a list of dicts: [{req_id, description, priority}]
    Falls back to [{"req_id": "UNLINKED", ...}] if nothing found.
    """
    parts = []
    if pr_description.strip():
        parts.append(f"PR Description:\n{pr_description}")
    if docstrings.strip():
        parts.append(f"Docstrings / comments:\n{docstrings}")
    if ticket_text.strip():
        parts.append(f"Ticket text:\n{ticket_text}")

    if not parts:
        return [{"req_id": "UNLINKED", "description": "No requirements source provided",
                 "priority": "LOW"}]

    user_message = "\n\n---\n\n".join(parts)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client  = anthropic.Anthropic(api_key=api_key)
    raw     = _call_claude(client, user_message)
    reqs    = _parse_requirements(raw)

    if not reqs:
        print("[RequirementsAnalyzer] No requirements found — using UNLINKED fallback")
        return [{"req_id": "UNLINKED",
                 "description": "Requirements could not be extracted from PR metadata",
                 "priority": "LOW"}]

    print(f"[RequirementsAnalyzer] Extracted {len(reqs)} requirement(s):")
    for r in reqs:
        print(f"  [{r['priority']}] {r['req_id']}: {r['description']}")

    return reqs


# ── CLI smoke test ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    sample_pr = """
    Fixes TICKET-101: add null-safety to the parser.
    Also addresses REQ-007 (input validation) and REQ-008 (error messages must be human-readable).
    The divide() function should raise ValueError (not ZeroDivisionError) when dividing by zero.
    """
    reqs = analyze_requirements(pr_description=sample_pr)
    print("\nResult:")
    print(json.dumps(reqs, indent=2))
