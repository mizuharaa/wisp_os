#!/usr/bin/env python3
"""Print exactly why one mission's delivery lane (review/tests/commit) is
stuck, instead of guessing from a single opaque fingerprint hash.

Usage:
    python dashboard/delivery_debug.py <cid>

Reuses delivery.py's own gate logic (never a re-implementation) and adds one
thing the dashboard doesn't show: a per-path breakdown of the reviewed files,
so a stale review or a commit blocked on pre-existing paths is traceable to
the exact file instead of a hash mismatch.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import delivery as dl  # noqa: E402


def _find_record(cid):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for sub in ("state/ceo", "state/ceo/archive"):
        path = os.path.join(root, *sub.split("/"), cid + ".json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle), path
    return None, None


def _path_fingerprint(repo, path):
    digest = hashlib.sha256()
    dl._digest_worktree_path(digest, repo, path)
    return digest.hexdigest()[:24]


def main(cid):
    record, path = _find_record(cid)
    if not record:
        print("no mission record found for %r under state/ceo" % cid)
        return 1
    print("mission: %s  (%s)" % (record.get("name") or cid, path))
    print("status:  %s" % record.get("status"))

    baseline = record.get("git_baseline") or {}
    print("\n-- baseline (captured at mission start) --")
    for key in ("repo_root", "head", "branch", "clean", "fingerprint"):
        print("  %-10s %s" % (key + ":", baseline.get(key)))
    dirty = baseline.get("dirty_paths") or []
    print("  dirty_paths (%d) -- these are excluded from auto-commit forever:"
          % len(dirty))
    for p in dirty:
        print("    - %s" % p)

    try:
        repo = dl._repo_root(record.get("workdir") or "")
    except dl.DeliveryError as exc:
        print("\nrepo lookup failed: %s" % exc)
        return 1
    snap = dl._snapshot(repo)
    print("\n-- current repository state --")
    print("  repo: %s" % repo)
    for key in ("head", "branch", "clean", "fingerprint"):
        print("  %-10s %s" % (key + ":", snap[key]))
    print("  changed paths (%d):" % len(snap["paths"]))
    for p in snap["paths"]:
        print("    - %s" % p)

    delivery = record.get("delivery") or {}
    review = delivery.get("review") or {}
    tests = delivery.get("tests") or {}
    commit = delivery.get("commit") or {}
    print("\n-- delivery lane --")
    print("  delivery.status: %s   changed: %s" %
          (delivery.get("status"), delivery.get("changed")))
    print("  review.status:   %s" % review.get("status"))
    print("  tests.status:    %s" % tests.get("status"))
    print("  commit.status:   %s" % commit.get("status"))
    if commit.get("blocked_reason"):
        print("  commit.blocked_reason: %s" % commit["blocked_reason"])
    if commit.get("error"):
        print("  commit.error: %s" % commit["error"])

    reviewed = dl._reviewed_paths(review)
    preexisting = set(dirty)
    print("\n-- per-path breakdown (%d reviewed paths) --" % len(reviewed))
    for p in reviewed:
        fp = _path_fingerprint(repo, p)
        flags = []
        full = os.path.normpath(os.path.join(repo, *p.split("/")))
        if not os.path.exists(full):
            flags.append("MISSING on disk now")
        if p in preexisting:
            flags.append("PRE-EXISTING -> excluded from commit")
        print("  %-60s now=%s  %s" % (p, fp, " ".join(flags)))

    if reviewed:
        gate = dl._fingerprint(repo, snap["branch"], reviewed)
        print("\n  live gate fingerprint over reviewed paths: %s" % gate)
        r_fp = review.get("fingerprint")
        print("  review recorded fingerprint:                %s  %s" %
              (r_fp, "MATCH" if r_fp == gate else "MISMATCH -> review is stale"))
        t_fp = tests.get("fingerprint")
        if t_fp:
            print("  tests recorded fingerprint:                  %s  %s" %
                  (t_fp, "MATCH" if t_fp == gate else "MISMATCH -> tests are stale"))

    check = dl._git(repo, "diff", "--check", "HEAD")
    print("\n-- git diff --check HEAD (whitespace / conflict-marker gate) --")
    print((check.get("stdout") or check.get("stderr") or "").strip() or "(clean)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python dashboard/delivery_debug.py <cid>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
