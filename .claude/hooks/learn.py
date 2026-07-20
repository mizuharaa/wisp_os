#!/usr/bin/env python3
"""Stop hook: the brain's automatic write path. recall.py reads the brain on
every prompt; this closes the loop by WRITING to it after every turn — no one
has to remember `hermes.py note` anymore.

How: read the session transcript, take the last operator-prompt -> assistant
exchange, ask Haiku "was anything durable learned?" (JSON verdict). If yes and
Hermes doesn't already know it, write the note (which also mirrors an Obsidian
card) and emit a `learn` event on the wire so the dashboard shows the brain
growing.

Registered in the GLOBAL ~/.claude/settings.json so every project feeds the
brain, and path-independent: everything resolves relative to this file.
Fails silent — a learning pass must never break a session.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
HERMES = os.path.join(ROOT, "hermes", "hermes.py")
MIRROR = os.path.join(HERE, "mirror.py")
SEEN = os.path.join(ROOT, "state", "learn-seen.json")
API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"  # ponytail: judgment call is cheap; escalate never

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from hermes import hermes as hermes_store

SYSTEM = """You extract durable learnings from one exchange between an operator \
and a coding agent, for a solved-problems knowledge base. A learning is worth \
keeping ONLY if it would save real future work: a non-obvious root cause, a \
fixed gotcha, a working recipe/config won through debugging, a constraint \
discovered the hard way. Routine edits, answers from general knowledge, plans, \
and status chatter are NOT learnings.
Reply with JSON only, no prose:
{"learned": true, "problem": "<one searchable line>", "solution": "<1-3 concrete lines>", "tags": "tag1,tag2"}
or {"learned": false}"""


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


def last_exchange(transcript_path):
    """Last real operator prompt and all assistant text after it."""
    prompt, answer = None, []
    try:
        f = open(transcript_path, encoding="utf-8", errors="ignore")
    except OSError:
        return None, ""
    with f:
        for line in f:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = row.get("message") or {}
            c = msg.get("content")
            if row.get("type") == "user":
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue  # mid-turn tool result, not an operator prompt
                    text = "\n".join(b.get("text", "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                if text.strip() and "<command-name>" not in text and "<local-command" not in text:
                    prompt, answer = text.strip(), []
            elif row.get("type") == "assistant" and isinstance(c, list):
                answer += [b.get("text", "") for b in c
                           if isinstance(b, dict) and b.get("type") == "text"]
    return prompt, "\n".join(a for a in answer if a).strip()


def already_known(problem):
    """Skip only near-duplicates — Hermes' 0.34 hit bar is too loose for dedup."""
    try:
        r = subprocess.run([sys.executable, HERMES, "query", problem[:300]],
                           capture_output=True, text=True, timeout=15)
        m = re.search(r"HIT (\d\.\d\d)", r.stdout or "")
        return bool(m and float(m.group(1)) >= 0.75)
    except Exception:
        return False


def judge(prompt, answer, key):
    body = json.dumps({
        "model": MODEL, "max_tokens": 400, "system": SYSTEM,
        "messages": [{"role": "user", "content":
                      "## Operator prompt\n%s\n\n## Agent's final answer\n%s"
                      % (prompt[:3000], answer[:5000])}]}).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    txt = "".join(b.get("text", "") for b in (data.get("content") or [])
                  if b.get("type") == "text")
    return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    sid = data.get("session_id") or "?"
    prompt, answer = last_exchange(data.get("transcript_path") or "")
    if not prompt or len(answer) < 300:  # nothing substantial happened
        return 0
    # one judgment per exchange, even if Stop fires again (global+project hooks)
    ph = hashlib.sha1((sid + prompt).encode("utf-8")).hexdigest()
    try:
        seen = json.load(open(SEEN, encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        seen = {}
    if seen.get(sid) == ph:
        return 0
    # Stop hooks can fire across thousands of sessions.  Keep only a bounded
    # idempotency window; this operational cache is not durable knowledge.
    seen.pop(sid, None)
    seen[sid] = ph
    seen = dict(list(seen.items())[-512:])
    try:
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        temp = SEEN + ".tmp.%s" % os.getpid()
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, SEEN)
    except OSError:
        pass
    key = _api_key()
    if not key:
        return 0
    try:
        v = judge(prompt, answer, key)
    except Exception:
        return 0
    if not (isinstance(v, dict) and v.get("learned") and v.get("problem") and v.get("solution")):
        return 0
    try:
        result = hermes_store.note_memory(
            v["problem"][:200], v["solution"][:600],
            (v.get("tags") or "auto")[:80], source="auto-learn")
        outcome = str(result.get("outcome") or "rejected")
        reason = str(result.get("reason_code") or "unknown")
        event = "learn" if outcome in ("accepted", "merged") else "learn-policy"
        subprocess.run([sys.executable, MIRROR, "--event", event,
                        "--detail", ("brain %s (%s): %s" %
                                     (outcome, reason, v["problem"]))[:180]],
                        capture_output=True, timeout=10)
    except Exception:
        pass
    return 0


def _selftest():
    import tempfile
    rows = [
        {"type": "user", "message": {"content": "fix the spotify seek 401"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "looking"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed: token lacked the modify scope; re-auth with user-modify-playback-state." }]}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r) for r in rows))
        path = f.name
    p, a = last_exchange(path)
    os.unlink(path)
    assert p == "fix the spotify seek 401", p
    assert "modify scope" in a and "looking" in a, a
    assert os.path.exists(HERMES), HERMES
    print("learn.py OK — key present:", bool(_api_key()))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
