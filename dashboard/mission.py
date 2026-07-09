#!/usr/bin/env python3
"""Mission intake: the "tell Maestro what to do" bar. One Sonnet call turns a
raw goal into either ONE clarifying question back to the operator, or a
launch-ready mission brief (structured JSON). serve.py hands a "launch" straight
to the orchestrator loop (worker -> opus critic -> accept/revise), which now
writes a Hermes note to the brain on acceptance. Stdlib only, like the rest.
"""
import json
import urllib.request

import chat  # API key resolution + the Maestro context snapshot

API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5"  # conscious spend: intake needs judgment, not opus

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["clarify", "launch"]},
        "question": {"type": "string"},   # when clarify: ONE short question
        "mission": {"type": "string"},    # when launch: the improved brief
        "name": {"type": "string"},       # 2-4 word mission name
        "turns": {"type": "integer"},     # worker turn budget 10-80
        "rounds": {"type": "integer"},    # critic rounds 1-5
        "model": {"type": "string", "enum": ["default", "haiku", "sonnet", "opus"]},
        "dir": {"type": "string"},        # absolute workdir if the goal names one
    },
    "required": ["action", "question", "mission", "name", "turns", "rounds",
                 "model", "dir"],
    "additionalProperties": False,
}

SYSTEM = """You are Maestro's mission intake. The operator types a goal into the \
command bar; you turn it into a mission for an autonomous worker->critic loop \
(headless Claude Code sessions in this repo, with a roster of specialist agents \
in .claude/agents it can /spawn, skills, the event wire, and the Hermes brain).

Decide ONE of:
- action="clarify": ONLY if the goal is critically ambiguous (no way to know the \
target project/path, or the outcome is unmeasurable). Ask ONE short, concrete \
question. Prefer launching with sensible assumptions over interrogating.
- action="launch": rewrite the goal as `mission` — a self-contained brief the \
worker can execute with no operator present: the goal, concrete steps, \
constraints, and a CHECKABLE definition of done. Fold in relevant context from \
the system snapshot below. End the mission with: 'When done, record what you \
learned: python hermes/hermes.py note "<problem>" "<solution>" --tags mission'.

Also set: name (2-4 words), turns (10-80: small for lookups, big for builds), \
rounds (1-5 critic iterations), model (haiku=mechanical, sonnet=most work, \
opus=hard reasoning only), dir (absolute path ONLY if the operator names a \
specific project outside this repo, else empty string).

For clarify: fill mission/name/dir with empty strings, turns/rounds with 0, model "default".
"""


def intake(text, history=None):
    """One intake turn. history = prior [{role,content}] so a clarify answer
    keeps its context. Returns the parsed schema dict or {"error": ...}."""
    key = chat._api_key()
    if not key:
        return {"error": "no ANTHROPIC_API_KEY (set it in the environment or .env)"}
    if not (text or "").strip():
        return {"error": "empty goal"}
    msgs = [{"role": m["role"], "content": str(m["content"])[:4000]}
            for m in (history or []) if m.get("role") in ("user", "assistant")][-8:]
    msgs.append({"role": "user", "content": text[:4000]})
    body = json.dumps({
        # max_tokens covers adaptive THINKING + the JSON; 1200 truncated mid-brief
        "model": MODEL, "max_tokens": 3500,
        "system": SYSTEM + "\n\n## System snapshot\n" + chat._context(),
        "messages": msgs,
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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
        if data.get("stop_reason") == "max_tokens":
            return {"error": "intake ran out of tokens — try a shorter goal"}
        return {"error": "intake returned malformed JSON"}


if __name__ == "__main__":
    # self-check: schema is valid JSON-serializable, key present, prompt sane
    json.dumps(SCHEMA)
    assert "clarify" in SYSTEM and "launch" in SYSTEM
    print("mission.py OK — key present:", bool(chat._api_key()))
