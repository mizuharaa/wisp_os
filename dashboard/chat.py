#!/usr/bin/env python3
"""The dashboard chat agent: a Haiku/Sonnet assistant that answers questions
about Maestro, the brain (vault + Hermes), and general topics. Stdlib only —
calls the Anthropic Messages API over urllib (no SDK, matching the repo's
no-dependency rule).

Key resolution: ANTHROPIC_API_KEY from the environment, else <ROOT>/.env
(gitignored — copy the VALUE in, never a path). Light questions go to Haiku;
heavier reasoning to Sonnet (the conductor's own "least-privilege spend").
"""
import json
import os
import re
import urllib.request
import uuid

from memory import recall_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.anthropic.com/v1/messages"
HAIKU, SONNET = "claude-haiku-4-5", "claude-sonnet-5"
# heavier reasoning -> Sonnet; everything else -> Haiku (cheap light default)
HEAVY = re.compile(r"\b(why|how does|architecture|design|debug|trace|compare|"
                   r"refactor|explain in detail|walk me through)\b", re.I)


def _api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k.strip()
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            m = re.match(r"\s*ANTHROPIC_API_KEY\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def structured(model, system, user, schema, max_tokens=4000, timeout=120):
    """One structured-output Messages call. Returns parsed dict or {"error"}."""
    key = _api_key()
    if not key:
        return {"error": "no ANTHROPIC_API_KEY (set it in the environment or .env)"}
    body = {"model": model, "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user[:12000]}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}}}
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"), headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": "API %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])}
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:200]}
    txt = "".join(b.get("text", "") for b in (data.get("content") or [])
                  if b.get("type") == "text")
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return {"error": "malformed JSON from " + model}


def _read(path, limit=1600):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="ignore") as handle:
            return handle.read()[:limit]
    except OSError:
        return ""


def _tail(path, n):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()]
        return rows[-n:]
    except (OSError, json.JSONDecodeError):
        return []


def _context(recall_block=""):
    """A compact snapshot of Maestro for the assistant's system prompt.

    Brain cards are query-selected instead of attaching recent cards to every
    turn: recency is not relevance, and unrelated cards waste context.
    """
    parts = ["You are the in-dashboard assistant for Maestro, a personal AI "
             "operating system (a 'conductor' that keeps memory fresh, reuses "
             "skills, and spawns specialist agents). Answer questions about the "
             "software, the brain (Obsidian vault + Hermes solved-problem log), "
             "and general topics. Be concise and direct; lead with the answer.",
             "\n\n## Soul (identity)\n" + _read("soul/soul.md", 1400)]
    try:
        with open(os.path.join(ROOT, "skills", "registry.json"), encoding="utf-8") as handle:
            reg = json.load(handle)
        sk = ", ".join("%s(%s)" % (n, v.get("status", "?"))
                       for n, v in (reg.get("skills") or {}).items())
        parts.append("\n\n## Skills\ngoal: %s\n%s" % (reg.get("goal", "-"), sk))
    except (OSError, json.JSONDecodeError):
        pass
    solved = []  # query-specific recall is appended below
    if solved:  # backward-compatible shape; query-specific recall is used now
        parts.append("\n\n## Brain — recent solved problems (Hermes)\n"
                     + "\n".join("- " + (s.get("problem", "")[:120]) for s in solved))
    wire = _tail("state/events.jsonl", 12)
    if wire:
        parts.append("\n\n## Recent activity (the wire)\n"
                     + "\n".join("- %s %s: %s" % (e.get("session", "?"), e.get("event", "?"),
                                                  (e.get("detail", "") or "")[:90]) for e in wire))
    if recall_block:
        parts.append(recall_block)
    return "".join(parts)


def ask(message, history=None, model=None, recall_context=None):
    """One chat turn with deterministic, receipted brain retrieval."""
    key = _api_key()
    if not key:
        return {"error": "no ANTHROPIC_API_KEY (set it in the environment or state .env)"}
    if not (message or "").strip():
        return {"error": "empty message"}
    meta = recall_context if isinstance(recall_context, dict) else {}
    cid = str(meta.get("cid") or ("chat-" + uuid.uuid4().hex[:12]))
    try:
        recall = recall_engine.query(
            message, root=ROOT, cid=cid,
            route=str(meta.get("route") or "dashboard_chat"),
            injected_into="dashboard_chat.system", injected_prompt_count=0)
    except Exception:
        # Brain telemetry is evidence, not a reason to make chat unavailable.
        # The shared engine normally converts ranker failures into error
        # receipts; this is the final guard for a state/lock failure.
        recall = {"prompt_block": "", "receipt": None}
    receipt = recall.get("receipt") if isinstance(recall, dict) else None
    model = model or (SONNET if (HEAVY.search(message) or len(message) > 240) else HAIKU)
    msgs = [{"role": m["role"], "content": str(m["content"])[:6000]}
            for m in (history or []) if m.get("role") in ("user", "assistant")][-10:]
    msgs.append({"role": "user", "content": message[:6000]})
    body = json.dumps({"model": model, "max_tokens": 1024,
                       "system": _context(recall.get("prompt_block") or ""),
                       "messages": msgs}).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": "2023-06-01"})
    try:
        if isinstance(receipt, dict) and recall.get("prompt_block"):
            receipt = recall_engine.mark_exposure(receipt, root=ROOT, prompt_count=1)
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        return {"error": "API %s: %s" % (e.code, detail), "model": model,
                "recall_receipt": receipt}
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:200], "model": model,
                "recall_receipt": receipt}
    reply = "".join(b.get("text", "") for b in (data.get("content") or [])
                    if b.get("type") == "text").strip()
    return {"reply": reply or "(no text in response)", "model": model,
            "recall_receipt": receipt}


if __name__ == "__main__":
    # self-check: context builds, model routing works, request is well-formed
    assert HAIKU in ("claude-haiku-4-5",)
    assert (SONNET if HEAVY.search("why does the guard block writes?") else HAIKU) == SONNET
    assert (SONNET if HEAVY.search("hi") else HAIKU) == HAIKU
    ctx = _context()
    assert "Maestro" in ctx and len(ctx) > 200, "context too thin"
    print("chat.py OK — key present:", bool(_api_key()), "| context chars:", len(ctx))
