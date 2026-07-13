#!/usr/bin/env python3
"""Grading + audit (ARCHITECTURE.md component 4).

Grades a finished task and writes an append-only audit line for the decision.
One completed task in -> a `grade` (the architect's contract, extended with a
letter, percentage, pass/fail, and the auto/manual gate) + one audit line out.

  score (0-1, from the contract) -> percent -> letter grade + pass/fail
  percent >= AUTO_THRESHOLD_PCT   -> auto-execution eligible (orchestration reads
  the `auto_eligible` bool); below it -> manual review.

Every graded task lands in state/audit.jsonl (append-only JSON lines). Stdlib
only. Run `python grader.py` for the self-check (grades, scores, the 92.9/93.0
boundary, and the audit-log guarantee).
"""
import datetime
import json
import os
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG = os.path.join(ROOT, "state", "audit.jsonl")

# The one gate: >= this percent is auto-execution eligible; below it is manual
# review. Single source of truth -- also the lower bound of an "A" grade, so an
# A-or-better task is exactly an auto-eligible one.
AUTO_THRESHOLD_PCT = 93.0
# A passing grade is anything above an F. Distinct from the auto gate above.
PASS_PCT = 60.0

# (lower bound percent, letter) -- first band the score clears wins.
_BANDS = [
    (97, "A+"), (AUTO_THRESHOLD_PCT, "A"), (90, "A-"),
    (87, "B+"), (83, "B"), (80, "B-"),
    (77, "C+"), (73, "C"), (70, "C-"),
    (67, "D+"), (63, "D"), (60, "D-"),
]


def letter_grade(pct):
    for lo, letter in _BANDS:
        if pct >= lo:
            return letter
    return "F"


def grade(task, grader="opus"):
    """Grade a finished `task` (must carry `score`, 0-1 per the grade contract).
    Returns a `grade`: the architect's fields (task_id, score, verdict, grader,
    ts) plus letter/percent/passed and the `auto_eligible` gate. Also appends
    the audit line -- grading and audit are one step, every grade is auditable."""
    score = float(task["score"])
    pct = round(score * 100, 1)
    passed = pct >= PASS_PCT
    auto = pct >= AUTO_THRESHOLD_PCT
    # verdict in the architect's vocabulary: pass=auto-eligible, revise=manual
    # but salvageable, fail=below passing.
    verdict = "pass" if auto else ("revise" if passed else "fail")
    g = {
        "task_id": task["id"],
        "score": score,
        "percent": pct,
        "letter": letter_grade(pct),
        "passed": passed,
        "auto_eligible": auto,             # <-- the boolean orchestration consumes
        "decision": "auto" if auto else "manual",
        "verdict": verdict,
        "grader": grader,
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _audit(g)
    return g


def _audit(g):
    """Append one line to the audit log: id, task, grade, score, pass/fail, the
    auto/manual decision, and a timestamp. Append-only -- never rewritten."""
    entry = {
        "id": uuid.uuid4().hex[:8],
        "task_id": g["task_id"],
        "grade": g["letter"],
        "score": g["percent"],
        "passed": g["passed"],
        "decision": g["decision"],
        "ts": g["ts"],
    }
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# --- self-check --------------------------------------------------------------

def _demo():
    """Prove the three DONE-WHEN conditions offline, in a temp audit log."""
    import tempfile
    global AUDIT_LOG
    AUDIT_LOG = os.path.join(tempfile.mkdtemp(prefix="grader-demo-"), "audit.jsonl")

    # (1) sample tasks -> correct grades + scores ----------------------------
    samples = [
        ({"id": "t_ap", "score": 0.99}, "A+", True,  "auto"),
        ({"id": "t_a",  "score": 0.95}, "A",  True,  "auto"),
        ({"id": "t_b",  "score": 0.85}, "B",  True,  "manual"),
        ({"id": "t_c",  "score": 0.72}, "C-", True,  "manual"),
        ({"id": "t_f",  "score": 0.40}, "F",  False, "manual"),
    ]
    for task, exp_letter, exp_pass, exp_dec in samples:
        g = grade(task)
        assert g["letter"] == exp_letter, (task["id"], g["letter"], exp_letter)
        assert g["passed"] == exp_pass, (task["id"], g["passed"])
        assert g["decision"] == exp_dec, (task["id"], g["decision"])
        assert g["percent"] == round(task["score"] * 100, 1)
        print("  %-6s %5.1f%%  %-2s  %-6s  pass=%s" %
              (task["id"], g["percent"], g["letter"], g["decision"], g["passed"]))

    # (2) the gate, asserted at the boundary: 92.9 manual / 93.0 auto --------
    below = grade({"id": "t_929", "score": 0.929})
    at = grade({"id": "t_930", "score": 0.930})
    assert below["auto_eligible"] is False and below["decision"] == "manual", below
    assert at["auto_eligible"] is True and at["decision"] == "auto", at
    print("  boundary: 92.9%% -> %s | 93.0%% -> %s" % (below["decision"], at["decision"]))

    # (3) every graded task appears in the append-only audit log -------------
    with open(AUDIT_LOG, encoding="utf-8") as f:
        logged = [json.loads(line) for line in f]
    graded_ids = [t["id"] for t, *_ in samples] + ["t_929", "t_930"]
    assert [e["task_id"] for e in logged] == graded_ids, "one audit line per grade, in order"
    for e in logged:
        assert set(e) >= {"id", "task_id", "grade", "score", "passed", "decision", "ts"}
    print("  audit log: %d/%d graded tasks recorded" % (len(logged), len(graded_ids)))

    print("OK - grades, boundary gate, and audit log all verified")


if __name__ == "__main__":
    _demo()
