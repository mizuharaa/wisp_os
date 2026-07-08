#!/usr/bin/env python3
"""Maestro bootstrap: verify every layer with real commands.

  python bootstrap.py         run all layer checks (exit 1 on any FAIL)
  python bootstrap.py boot    run the CLAUDE.md boot sequence (vault, skills, announce)
"""
import json
import os
import re
import subprocess
import sys
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(ok)
    print("[%s] %-12s %s" % ("PASS" if ok else "FAIL", label, detail))


def run(*args, stdin=None):
    return subprocess.run([PY] + list(args), cwd=ROOT, input=stdin,
                          capture_output=True, text=True)


def main():
    # L0 soul
    soul = os.path.join(ROOT, "soul", "soul.md")
    txt = open(soul, encoding="utf-8").read() if os.path.exists(soul) else ""
    beliefs = len(re.findall(r"^\d+\.\s+\*\*", txt, re.M))
    check("L0 soul", beliefs >= 5 and os.path.exists(os.path.join(ROOT, "soul", "CHANGELOG.md")),
          "%d beliefs + changelog" % beliefs)

    # L1 inventory
    cm = os.path.join(ROOT, "CLAUDE.md")
    ok = os.path.exists(cm) and "Boot sequence" in open(cm, encoding="utf-8").read()
    check("L1 inventory", ok, "CLAUDE.md boot sequence present")

    # L2 rules: guard suite + wire append
    g = run(os.path.join(".claude", "hooks", "test_guard.py"))
    check("L2 guard", g.returncode == 0, "guard self-test (7 cases)")
    events = os.path.join(ROOT, "state", "events.jsonl")
    before = sum(1 for _ in open(events, encoding="utf-8")) if os.path.exists(events) else 0
    m = run(os.path.join(".claude", "hooks", "mirror.py"), "--event", "bootstrap", "--detail", "verify wire")
    after = sum(1 for _ in open(events, encoding="utf-8"))
    check("L2 wire", m.returncode == 0 and after == before + 1, "event appended to events.jsonl")

    # L3 skills
    l = run(os.path.join("skills", "engine.py"), "list")
    check("L3 skills", l.returncode == 0 and "GOAL:" in l.stdout, "engine lists registry")
    lp = run(os.path.join("skills", "loop-engineering", "loop.py"),
             "--doer", "%s -c \"pass\"" % PY, "--goal", "%s -c \"pass\"" % PY,
             "--max", "2", "--label", "bootstrap-loop")
    check("L3 loop", lp.returncode == 0, "critic->doer loop reaches goal")

    # L4 agents + commands
    agents = os.listdir(os.path.join(ROOT, ".claude", "agents"))
    cmds = os.listdir(os.path.join(ROOT, ".claude", "commands"))
    check("L4 agents", len(agents) >= 9 and len(cmds) >= 10,
          "%d agents, %d commands" % (len(agents), len(cmds)))

    # L5 wires
    v = run(os.path.join("memory", "pipeline.py"), "vault")
    check("L5 vault", v.returncode == 0, (v.stdout.splitlines() or ["?"])[0])
    naked = run(os.path.join("memory", "pipeline.py"), "write", "naked fact")
    check("L5 no-rot", naked.returncode != 0, "naked fact refused (source required)")

    # hermes
    hit = run(os.path.join("hermes", "hermes.py"), "query", "block a tool call from a hook")
    miss = run(os.path.join("hermes", "hermes.py"), "query", "zzz-" + uuid.uuid4().hex)
    check("hermes", hit.returncode == 0 and miss.returncode == 1, "known problem hits, nonce misses")

    # dashboard
    ok = os.path.exists(os.path.join(ROOT, "dashboard", "index.html"))
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8817/dashboard/", timeout=2) as r:
            live = " (server LIVE, HTTP %d)" % r.status
    except Exception:
        live = " (server not running -- python dashboard/serve.py)"
    check("dashboard", ok, "index.html + serve.py" + live)

    # hygiene: substrate purity
    needle = "jar" + "vis"
    dirty = []
    for dirpath, dirs, files in os.walk(ROOT):
        # .appwindow is Edge's app-mode browser profile (desktop.py) — browser
        # junk, not Maestro's writing; it can legitimately contain any string.
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".appwindow")]
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                if needle in open(p, encoding="utf-8", errors="ignore").read().lower():
                    dirty.append(os.path.relpath(p, ROOT))
            except OSError:
                pass
    check("hygiene", not dirty, "zero consumer references" if not dirty else "found in: %s" % dirty)

    print("\n%d/%d checks passed" % (sum(RESULTS), len(RESULTS)))
    return 0 if all(RESULTS) else 1


def boot():
    print("== Maestro boot ==")
    print("[1/3] soul: read soul/soul.md (conductor identity)")
    for label, args in (
        ("[2/3] vault", [os.path.join("memory", "pipeline.py"), "vault"]),
        ("[3/3] skills", [os.path.join("skills", "engine.py"), "list"]),
    ):
        print(label)
        r = run(*args)
        print("\n".join("   " + ln for ln in r.stdout.strip().splitlines()))
        if r.returncode != 0:
            print("BOOT FAILED at " + label)
            return 1
    run(os.path.join(".claude", "hooks", "mirror.py"), "--stage", "think", "--detail", "session online")
    print("announced to dashboard. Boot clean.")
    return 0


if __name__ == "__main__":
    sys.exit(boot() if "boot" in sys.argv[1:] else main())
