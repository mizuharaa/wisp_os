#!/usr/bin/env python3
"""Guarded post-mission delivery: review -> tests -> commit -> push.

The browser supplies only a mission id, an allowlisted action, an optional
commit message, and (for push) a one-use confirmation token. Repository paths,
commands, changed files, branches, and remotes are always derived locally.
"""
import datetime
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

import runtime as agent_runtime


IS_WIN = sys.platform == "win32"
REPORT_LIMIT = 16_000
OUTPUT_LIMIT = 12_000
PUSH_TOKEN_TTL = 5 * 60
SUCCESS_STATES = frozenset(("done", "completed", "success", "succeeded", "skipped"))
TEST_PROJECT_SCAN_DEPTH = 3
TEST_PROJECT_IGNORES = frozenset((
    "__pycache__", "build", "dist", "node_modules", "vendor", "venv",
))


class DeliveryError(RuntimeError):
    pass


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _redact(value, limit=OUTPUT_LIMIT):
    text = str(value or "")
    text = agent_runtime.SECRET_RE.sub(
        lambda match: match.group(1) + "=<redacted>", text)
    return text[:max(0, int(limit))]


def _run(argv, cwd, timeout=60, env=None):
    """Run one argv-only command with bounded output and process-tree timeout."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(item, str) and item for item in argv):
        raise DeliveryError("invalid delivery command")
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    proc = None
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", shell=False,
            env=merged_env, start_new_session=not IS_WIN,
            creationflags=((getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                            getattr(subprocess, "CREATE_NO_WINDOW", 0))
                           if IS_WIN else 0))
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            agent_runtime.terminate_process_tree(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                stdout, stderr = "", ""
            return {"returncode": 124, "stdout": _redact(stdout),
                    "stderr": _redact((stderr or "") +
                                       "\nTimed out after %ss." % timeout)}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"returncode": 127, "stdout": "",
                "stderr": "%s failed to start: %s" % (argv[0], str(exc)[:500])}
    return {"returncode": int(getattr(proc, "returncode", 0) or 0),
            "stdout": _redact(stdout), "stderr": _redact(stderr)}


def _git(repo, *args, timeout=60):
    return _run(["git", "-C", repo] + [str(arg) for arg in args], repo,
                timeout=timeout, env={"GIT_TERMINAL_PROMPT": "0"})


def _require(result, label):
    if result["returncode"]:
        detail = (result.get("stderr") or result.get("stdout") or
                  "%s failed" % label).strip()
        raise DeliveryError("%s: %s" % (label, detail[:1200]))
    return str(result.get("stdout") or "").strip()


def _require_raw(result, label):
    """Validate a command while preserving path/status bytes decoded as text."""
    if result["returncode"]:
        detail = (result.get("stderr") or result.get("stdout") or
                  "%s failed" % label).strip()
        raise DeliveryError("%s: %s" % (label, detail[:1200]))
    return str(result.get("stdout") or "")


def _repo_root(workdir):
    if not str(workdir or "").strip():
        # realpath("") is the server's own cwd — delivery must never silently
        # attribute a mission without a workdir against an unrelated repo.
        raise DeliveryError("mission has no recorded working directory")
    run_dir = os.path.normpath(os.path.realpath(str(workdir)))
    if not os.path.isdir(run_dir):
        raise DeliveryError("mission working directory is unavailable")
    root = _require(_git(run_dir, "rev-parse", "--show-toplevel"),
                    "Git repository lookup")
    root = os.path.normpath(os.path.realpath(root))
    if not os.path.isdir(root):
        raise DeliveryError("resolved Git repository is unavailable")
    try:
        if os.path.commonpath((run_dir, root)) != root:
            raise DeliveryError("mission working directory left its Git repository")
    except ValueError as exc:
        raise DeliveryError("mission repository path is invalid") from exc
    return root


def _clean_path(value):
    value = str(value or "").replace("\\", "/").strip("/")
    if not value or value == ".." or value.startswith("../") or "/../" in value:
        return ""
    if os.path.isabs(value) or "\x00" in value:
        return ""
    return value


def _status_paths(raw):
    """Parse porcelain-v1 -z, including both sides of renames/copies."""
    records = str(raw or "").split("\x00")
    paths, index = [], 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4:
            continue
        code, path = record[:2], _clean_path(record[3:])
        if path:
            paths.append(path)
        if "R" in code or "C" in code:
            if index < len(records):
                old = _clean_path(records[index])
                index += 1
                if old:
                    paths.append(old)
    return sorted(set(paths))


def _digest_part(digest, value):
    digest.update(str(value).encode("utf-8", "replace"))
    digest.update(b"\0")


def _digest_worktree_path(digest, repo, relative):
    """Hash one changed path without following it outside the repository."""
    candidate = os.path.normpath(os.path.join(repo, *relative.split("/")))
    _digest_part(digest, relative)
    try:
        info = os.lstat(candidate)
    except OSError:
        _digest_part(digest, "missing")
        return
    _digest_part(digest, "%s:%s" % (info.st_mode, info.st_size))
    if os.path.islink(candidate):
        try:
            _digest_part(digest, "symlink:" + os.readlink(candidate))
        except OSError:
            _digest_part(digest, "unreadable-symlink")
        return
    root = os.path.normcase(os.path.realpath(repo))
    resolved = os.path.normcase(os.path.realpath(candidate))
    try:
        if os.path.commonpath((root, resolved)) != root:
            _digest_part(digest, "outside-repository")
            return
    except ValueError:
        _digest_part(digest, "invalid-path")
        return
    if os.path.isfile(candidate):
        try:
            with open(candidate, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            digest.update(b"\0")
        except OSError:
            _digest_part(digest, "unreadable-file")
    elif os.path.isdir(candidate):
        # A changed submodule is represented as a directory in the superproject.
        sub_head = _git(candidate, "rev-parse", "HEAD")
        _digest_part(digest, "directory:" + str(sub_head.get("returncode")))
        _digest_part(digest, sub_head.get("stdout") or sub_head.get("stderr") or "")
    else:
        _digest_part(digest, "special-file")


def _digest_index(digest, repo, paths):
    """Include staged blobs/modes for paths with split index/worktree states."""
    for offset in range(0, len(paths), 16):
        chunk = paths[offset:offset + 16]
        result = _git(repo, "ls-files", "--stage", "-z", "--", *chunk)
        _digest_part(digest, result.get("returncode"))
        _digest_part(digest, result.get("stdout") or result.get("stderr") or "")


def _fingerprint(repo, head, branch, paths):
    """Hash HEAD/branch plus the worktree and index state of a fixed path set.

    Scoping to a path list is what keeps review->test->commit stable while
    unrelated files (logs, caches, sync noise) churn elsewhere in the repo."""
    paths = sorted(set(paths))
    digest = hashlib.sha256()
    for value in (head, branch):
        _digest_part(digest, value)
    for relative in paths:
        _digest_worktree_path(digest, repo, relative)
    _digest_index(digest, repo, paths)
    return digest.hexdigest()[:24]


def _snapshot(repo):
    head_result = _git(repo, "rev-parse", "HEAD")
    head = (head_result.get("stdout") or "").strip() if not head_result["returncode"] else ""
    branch_result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = ((branch_result.get("stdout") or "").strip()
              if not branch_result["returncode"] else "")
    status = _require_raw(
        _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        "Git status")
    paths = _status_paths(status)
    return {"head": head, "branch": branch, "paths": paths,
            "clean": not bool(status),
            "fingerprint": _fingerprint(repo, head, branch, paths)}


def capture_git_baseline(workdir):
    """Capture the authoritative Git boundary before any mission worker runs."""
    try:
        repo = _repo_root(workdir)
        snap = _snapshot(repo)
    except DeliveryError as exc:
        return {"available": False, "captured_at": _now(),
                "reason": str(exc)[:500]}
    return {"available": True, "captured_at": _now(), "repo_root": repo,
            "head": snap["head"], "branch": snap["branch"],
            "clean": snap["clean"], "dirty_paths": snap["paths"],
            "fingerprint": snap["fingerprint"]}


def _blocked_reason(baseline):
    """Reasons automatic commit is impossible. A dirty start is NOT one of
    them: the baseline records which paths were already dirty, so the
    mission's own work is attributable — pre-existing paths are simply
    excluded from the commit set."""
    if not baseline or not baseline.get("available"):
        return ("This mission predates Git attribution or is not in a Git repository; "
                "review and tests are available, but automatic commit is disabled.")
    if baseline.get("legacy"):
        return ("This mission finished before Git attribution was captured. Review and "
                "tests are available, but Rune cannot safely choose files to commit.")
    if not baseline.get("head"):
        return "The mission started without a Git HEAD; automatic commit is disabled."
    return ""


def _head_is_published(repo, head):
    """Prove a clean legacy HEAD already equals its configured upstream ref."""
    upstream = _git(repo, "rev-parse", "--verify", "@{upstream}")
    return bool(not upstream["returncode"] and head and
                (upstream.get("stdout") or "").strip() == head)


def _reconcile_legacy_clean(mission, delivery):
    """Migrate a persisted legacy lane when Git proves there is no payload."""
    baseline = mission.get("git_baseline")
    if (not isinstance(baseline, dict) or not baseline.get("legacy") or
            not baseline.get("available")):
        return delivery
    try:
        repo = _repo_root(mission.get("workdir"))
        snap = _snapshot(repo)
    except DeliveryError:
        return delivery
    if (not snap["clean"] or not snap["head"] or
            snap["head"] != baseline.get("head") or
            not _head_is_published(repo, snap["head"])):
        return delivery
    commit = delivery.get("commit") or {}
    push = delivery.get("push") or {}
    if commit.get("status") == "committed" or push.get("status") == "pushed":
        return delivery
    delivery["changed"] = False
    delivery["verification_only"] = True
    delivery.pop("blocked_reason", None)
    delivery["commit"] = {"status": "not_needed"}
    delivery["push"] = {"status": "not_needed"}
    review = delivery.get("review") or {"status": "pending"}
    tests = delivery.get("tests") or {"status": "pending"}
    delivery["review"], delivery["tests"] = review, tests
    if tests.get("status") == "failed":
        delivery["status"] = "tests_failed"
    elif tests.get("status") == "passed":
        delivery["status"] = "tested"
    elif review.get("status") == "failed":
        delivery["status"] = "review_failed"
    elif review.get("status") == "reviewed":
        delivery["status"] = "reviewed"
    else:
        delivery["status"] = "needs_review"
    delivery["current"] = {
        "head": snap["head"], "branch": snap["branch"],
        "changed_paths": snap["paths"], "fingerprint": snap["fingerprint"],
    }
    return delivery


def initialize_completed_delivery(mission):
    """Attach a stable delivery lane to one successful persisted mission."""
    existing = mission.get("delivery")
    if isinstance(existing, dict) and existing.get("version") == 1:
        return _reconcile_legacy_clean(mission, existing)
    baseline = mission.get("git_baseline") if isinstance(
        mission.get("git_baseline"), dict) else None
    legacy = baseline is None or bool(baseline.get("legacy"))
    if baseline is None:
        baseline = capture_git_baseline(mission.get("workdir"))
        baseline["legacy"] = True
        mission["git_baseline"] = baseline
    available = bool(baseline.get("available"))
    current, repo, unavailable_reason = None, None, ""
    if available:
        try:
            repo = _repo_root(mission.get("workdir"))
            current = _snapshot(repo)
        except DeliveryError as exc:
            available = False
            unavailable_reason = str(exc)[:500]
    same_clean_head = bool(available and current and current["clean"] and
                           current["head"] == baseline.get("head"))
    no_change = bool(same_clean_head and not legacy)
    verification_only = bool(same_clean_head and legacy and repo and
                             _head_is_published(repo, current["head"]))
    no_delivery = no_change or verification_only
    delivery = {
        "version": 1, "available": available,
        "changed": not no_delivery,
        "status": ("clean" if no_change else
                   ("needs_review" if available else "unavailable")),
        "created_at": _now(),
        "review": {"status": "not_needed" if no_change else "pending"},
        "tests": {"status": "not_needed" if no_change else "pending"},
        "commit": {"status": "not_needed" if no_delivery else "pending"},
        "push": {"status": "not_needed" if no_delivery else "pending"},
    }
    if verification_only:
        delivery["verification_only"] = True
    elif not no_change:
        delivery["commit"]["blocked_reason"] = _blocked_reason(baseline)
    if current:
        delivery["current"] = {"head": current["head"],
                               "branch": current["branch"],
                               "changed_paths": current["paths"],
                               "fingerprint": current["fingerprint"]}
    if not available:
        delivery["reason"] = str(baseline.get("reason") or unavailable_reason or
                                 "Git delivery is unavailable")[:500]
    elif delivery["commit"].get("blocked_reason"):
        delivery["blocked_reason"] = delivery["commit"]["blocked_reason"]
    mission["delivery"] = delivery
    return delivery


def _state(mission):
    if str(mission.get("status") or "").lower() not in SUCCESS_STATES:
        raise DeliveryError("delivery is available only after a successful mission")
    delivery = initialize_completed_delivery(mission)
    if not delivery.get("available"):
        raise DeliveryError(delivery.get("reason") or "Git delivery is unavailable")
    baseline = mission.get("git_baseline") or {}
    repo = _repo_root(mission.get("workdir"))
    saved_root = os.path.normcase(os.path.realpath(str(baseline.get("repo_root") or "")))
    if not saved_root or saved_root != os.path.normcase(os.path.realpath(repo)):
        raise DeliveryError("mission Git baseline no longer matches its repository")
    return delivery, baseline, repo, _snapshot(repo)


def _invalidate_after_review(delivery):
    delivery["tests"] = {"status": "pending"}
    if delivery.get("changed") is False:
        delivery["commit"] = {"status": "not_needed"}
        delivery["push"] = {"status": "not_needed"}
    else:
        blocked = (delivery.get("commit") or {}).get("blocked_reason", "")
        delivery["commit"] = {"status": "pending", "blocked_reason": blocked}
        delivery["push"] = {"status": "pending"}


def _untracked_review(repo):
    """Render bounded source previews for files Git cannot show in `diff HEAD`."""
    raw = _require_raw(
        _git(repo, "ls-files", "--others", "--exclude-standard", "-z"),
        "untracked-file lookup")
    sections, used = [], 0
    root = os.path.normcase(os.path.realpath(repo))
    for value in raw.split("\x00"):
        relative = _clean_path(value)
        if not relative:
            continue
        full = os.path.realpath(os.path.join(repo, *relative.split("/")))
        try:
            if os.path.commonpath((root, os.path.normcase(full))) != root:
                continue
        except ValueError:
            continue
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as handle:
                data = handle.read(32_001)
        except OSError:
            continue
        if b"\x00" in data:
            body = "[binary file; %d bytes]" % size
        else:
            truncated = len(data) > 32_000
            source = data[:32_000].decode("utf-8", "replace")
            body = "\n".join("+" + line for line in source.splitlines())
            if truncated:
                body += "\n+[preview truncated]"
        section = ("UNTRACKED FILE %s (%d bytes)\n+++ %s\n%s" %
                   (relative, size, relative, body))
        remaining = REPORT_LIMIT - used
        if remaining <= 0:
            break
        sections.append(section[:remaining])
        used += len(sections[-1])
    return "\n\n".join(sections)


AI_REVIEW_MODEL = "claude-haiku-4-5"
AI_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "attention"]},
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"file": {"type": "string"},
                               "note": {"type": "string"}},
                "required": ["file", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "issues"],
    "additionalProperties": False,
}
AI_REVIEW_SYSTEM = """You review a completed agent mission's repository \
changes before a human operator commits them. Judge ONLY the diff report \
against the mission goal.

Verdict "approve" when the changes plausibly implement the goal and show no \
red flags. "attention" when the operator should look first: changes unrelated \
to the goal, deleted or overwritten existing work, leftover debug/temporary \
code, credentials or secrets in the diff, half-finished code, or a \
deliverable the goal names that is absent from the diff.

summary: one or two plain-language sentences saying what the change does and \
whether it is ready — written for a person, not a terminal. issues: up to 5 \
concrete {file, note} items; empty for approve."""


def _ai_review(mission, report):
    """Best-effort model read of the reviewed diff. Advisory, never a gate."""
    if os.environ.get("RUNE_DISABLE_AI_REVIEW") == "1":
        return None
    try:
        import chat  # lazy: resolved on the server's sys.path, needs its key
    except Exception:
        return None
    if not chat._api_key():
        return None
    goal = str(mission.get("goal") or mission.get("name") or "")[:2000]
    result = chat.structured(
        AI_REVIEW_MODEL, AI_REVIEW_SYSTEM,
        "MISSION GOAL:\n%s\n\nDIFF REPORT:\n%s" % (goal, report[:9000]),
        AI_REVIEW_SCHEMA, max_tokens=1000, timeout=90)
    if (not isinstance(result, dict) or result.get("error") or
            result.get("verdict") not in ("approve", "attention")):
        return None
    issues = [{"file": str(item.get("file") or "")[:200],
               "note": str(item.get("note") or "")[:300]}
              for item in (result.get("issues") or [])[:5]
              if isinstance(item, dict)]
    return {"verdict": result["verdict"],
            "summary": str(result.get("summary") or "")[:500],
            "issues": issues, "model": AI_REVIEW_MODEL}


def _review(mission):
    delivery, baseline, repo, snap = _state(mission)
    status = _git(repo, "status", "--short", "--untracked-files=all")
    stat = _git(repo, "diff", "--stat", "HEAD", timeout=120)
    check = _git(repo, "diff", "--check", "HEAD", timeout=120)
    patch_parts = []
    if baseline.get("head") and snap["head"] != baseline.get("head"):
        commits = _git(repo, "log", "--oneline", "--decorate=no",
                       "%s..HEAD" % baseline["head"], timeout=60)
        committed = _git(repo, "diff", "--no-ext-diff", "--unified=3",
                         "%s..HEAD" % baseline["head"], timeout=120)
        patch_parts.extend(("COMMITS SINCE START\n" + commits.get("stdout", ""),
                            "COMMITTED DIFF\n" + committed.get("stdout", "")))
    untracked = _untracked_review(repo)
    if untracked:
        patch_parts.append(untracked)
    working = _git(repo, "diff", "--no-ext-diff", "--unified=3", "HEAD", timeout=120)
    patch_parts.append("WORKTREE DIFF\n" + working.get("stdout", ""))
    preexisting = sorted(set(snap["paths"]) &
                         set(baseline.get("dirty_paths") or []))
    report = "\n\n".join(part.strip() for part in (
        "STATUS\n" + (status.get("stdout") or "clean"),
        "STAT\n" + (stat.get("stdout") or "no tracked diff"),
        ("PRE-EXISTING OPERATOR CHANGES (already dirty before the mission "
         "started; excluded from automatic commit)\n" + "\n".join(preexisting))
        if preexisting else "",
        "\n\n".join(patch_parts)) if part.strip())
    ai = _ai_review(mission, report)
    if ai:
        header = "AI REVIEW — %s\n%s" % (
            "needs attention" if ai["verdict"] == "attention" else "approve",
            ai["summary"])
        if ai["issues"]:
            header += "\n" + "\n".join("- %s: %s" % (item["file"], item["note"])
                                       for item in ai["issues"])
        report = header + "\n\n" + report
    review = {"status": "reviewed" if not check["returncode"] else "failed",
              "checked_at": _now(), "fingerprint": snap["fingerprint"],
              "head": snap["head"], "changed_paths": snap["paths"],
              "preexisting_paths": preexisting,
              "report": _redact(report, REPORT_LIMIT)}
    if ai:
        review["ai"] = ai
        review["detail"] = ("AI review: %s — %s" % (
            "needs attention" if ai["verdict"] == "attention" else "looks right",
            ai["summary"]))[:300]
    if check["returncode"]:
        review["error"] = _redact(check.get("stdout") or check.get("stderr") or
                                  "git diff --check failed", 2000)
    delivery["review"] = review
    _invalidate_after_review(delivery)
    delivery["review"] = review
    delivery["status"] = "reviewed" if review["status"] == "reviewed" else "review_failed"
    delivery["current"] = {"head": snap["head"], "branch": snap["branch"],
                           "changed_paths": snap["paths"],
                           "fingerprint": snap["fingerprint"]}
    return {"delivery": public_delivery(delivery)}


def _pytest_configured(directory, names):
    """Return whether one project directory explicitly configures pytest."""
    if "pytest.ini" in names:
        return True
    markers = {
        "pyproject.toml": ("[tool.pytest.",),
        "setup.cfg": ("[tool:pytest]", "[pytest]"),
        "tox.ini": ("[pytest]",),
    }
    for name, needles in markers.items():
        if name not in names:
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                value = handle.read(200_000).lower()
        except OSError:
            continue
        if any(needle in value for needle in needles):
            return True
    return False


def _contains_pytest_files(directory):
    """Inspect a conventional test directory without following dependency trees."""
    try:
        for current, dirs, files in os.walk(directory):
            dirs[:] = [
                name for name in dirs
                if name not in TEST_PROJECT_IGNORES and not name.startswith(".")
            ]
            if any(name.startswith("test") and name.endswith(".py")
                   for name in files):
                return True
    except OSError:
        pass
    return False


def _nested_pytest_targets(repo):
    """Find bounded, explicitly configured pytest projects in a monorepo."""
    root = os.path.normpath(os.path.realpath(repo))
    targets = []
    try:
        walker = os.walk(root)
        for current, dirs, files in walker:
            relative = os.path.relpath(current, root)
            depth = 0 if relative == "." else len(relative.split(os.sep))
            dirs[:] = [
                name for name in dirs
                if name not in TEST_PROJECT_IGNORES and not name.startswith(".")
            ]
            if depth >= TEST_PROJECT_SCAN_DEPTH:
                dirs[:] = []
            if depth == 0 or not _pytest_configured(current, set(files)):
                continue
            tests = os.path.join(current, "tests")
            if not os.path.isdir(tests) or not _contains_pytest_files(tests):
                continue
            target = os.path.relpath(tests, root).replace(os.sep, "/")
            project = os.path.relpath(current, root).replace(os.sep, "/")
            poetry = False
            if "pyproject.toml" in files:
                try:
                    with open(os.path.join(current, "pyproject.toml"),
                              encoding="utf-8") as handle:
                        poetry = "[tool.poetry]" in handle.read(200_000).lower()
                except OSError:
                    pass
            targets.append((target, project, poetry))
            # A configured project owns its test tree; do not treat fixtures or
            # vendored examples below it as independent projects.
            dirs[:] = []
    except OSError:
        return []
    return sorted(set(targets))


def _poetry_test_python(repo, project):
    """Resolve Poetry's existing venv without creating or installing one."""
    poetry = shutil.which("poetry")
    if not poetry:
        return ""
    result = _run([poetry, "-C", project, "env", "info", "--executable"],
                  repo, timeout=20)
    if result["returncode"]:
        return ""
    lines = [line.strip() for line in (result.get("stdout") or "").splitlines()
             if line.strip()]
    if not lines:
        return ""
    candidate = os.path.normpath(os.path.realpath(lines[-1]))
    executable_dir = os.path.dirname(candidate)
    if os.path.basename(executable_dir).lower() in ("bin", "scripts"):
        venv = os.path.dirname(executable_dir)
    else:
        venv = executable_dir
    if (not os.path.isfile(candidate) or
            not os.path.isfile(os.path.join(venv, "pyvenv.cfg"))):
        return ""
    probe = _run([candidate, "-c", "import pytest"], repo, timeout=20,
                 env={"PYTHONDONTWRITEBYTECODE": "1"})
    return candidate if not probe["returncode"] else ""


def _project_venv_python(repo, project):
    """Use a nested project's own .venv when it exists and can run pytest."""
    base = os.path.join(repo, *project.split("/"))
    candidate = os.path.join(base, ".venv", "Scripts" if IS_WIN else "bin",
                             "python.exe" if IS_WIN else "python")
    if (not os.path.isfile(candidate) or
            not os.path.isfile(os.path.join(base, ".venv", "pyvenv.cfg"))):
        return ""
    probe = _run([candidate, "-c", "import pytest"], repo, timeout=20,
                 env={"PYTHONDONTWRITEBYTECODE": "1"})
    return candidate if not probe["returncode"] else ""


def _override_test_argv(repo):
    """Read an operator-pinned test command from .rune-test.json in the repo.

    Returns (argv, cwd) or None. The file is local and operator-controlled —
    the same trust boundary as the workers that already run inside the repo."""
    path = os.path.join(repo, ".rune-test.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError) as exc:
        raise DeliveryError(".rune-test.json is unreadable: %s" % str(exc)[:200])
    argv = spec.get("argv") if isinstance(spec, dict) else None
    if (not isinstance(argv, list) or not argv or
            any(not isinstance(item, str) or not item for item in argv)):
        raise DeliveryError(
            '.rune-test.json must contain {"argv": ["command", ...]}')
    raw_cwd = str(spec.get("cwd") or "")
    cwd = _clean_path(raw_cwd)
    if raw_cwd and not cwd:
        raise DeliveryError(".rune-test.json cwd must stay inside the repository")
    full = os.path.normpath(os.path.join(repo, *cwd.split("/"))) if cwd else repo
    if cwd and not os.path.isdir(full):
        raise DeliveryError(".rune-test.json cwd does not exist: " + cwd)
    return list(argv), full


def detect_test_argv(repo):
    """Choose a fixed argv from repository markers; never accept browser input."""
    pyproject = ""
    try:
        with open(os.path.join(repo, "pyproject.toml"), encoding="utf-8") as handle:
            pyproject = handle.read(200_000).lower()
    except OSError:
        pass
    if (os.path.isfile(os.path.join(repo, "pytest.ini")) or
            os.path.isfile(os.path.join(repo, "conftest.py")) or
            "tool.pytest" in pyproject):
        if importlib.util.find_spec("pytest") is not None:
            return [sys.executable, "-m", "pytest", "-q"]
    has_unittest = False
    for directory in (repo, os.path.join(repo, "tests")):
        if os.path.isdir(directory):
            try:
                has_unittest = has_unittest or any(
                    name.startswith("test") and name.endswith(".py")
                    for name in os.listdir(directory))
            except OSError:
                pass
    if has_unittest:
        return [sys.executable, "-m", "unittest", "discover", "-v"]
    nested_pytest = _nested_pytest_targets(repo)
    if nested_pytest:
        if len(nested_pytest) > 1:
            raise DeliveryError(
                "Rune found multiple nested pytest projects and cannot safely "
                "choose one test scope")
        target, project, poetry_project = nested_pytest[0]
        python = _poetry_test_python(repo, project) if poetry_project else ""
        if not python:
            python = _project_venv_python(repo, project)
        if not python and importlib.util.find_spec("pytest") is not None:
            # Last resort; imports of the project's installed packages may
            # fail here — the test output explains how to pin a real command.
            python = sys.executable
        if not python:
            raise DeliveryError(
                "Rune found a nested pytest project but no trusted pytest "
                "interpreter is available")
        # `--` prevents a repository-controlled path from being interpreted as
        # a pytest option. Running from the Git root keeps the delivery snapshot
        # boundary unchanged while pytest discovers the nested project config.
        return [python, "-m", "pytest", "-q", "--", target]
    package = os.path.join(repo, "package.json")
    if os.path.isfile(package):
        try:
            with open(package, encoding="utf-8") as handle:
                scripts = (json.load(handle).get("scripts") or {})
            test_script = str(scripts.get("test") or "")
            if test_script and "no test specified" not in test_script.lower():
                return ["npm.cmd" if IS_WIN else "npm", "test"]
        except (OSError, ValueError, AttributeError):
            pass
    if os.path.isfile(os.path.join(repo, "Cargo.toml")):
        return ["cargo", "test"]
    if os.path.isfile(os.path.join(repo, "go.mod")):
        return ["go", "test", "./..."]
    raise DeliveryError("Rune could not detect a safe project test command")


def _reviewed_paths(review):
    """The exact path set the operator reviewed; delivery never widens it."""
    values = review.get("changed_paths") if isinstance(review, dict) else None
    return sorted({path for path in (_clean_path(item) for item in (values or []))
                   if path})


def _mark_review_stale(delivery, snap):
    """Persist why the review no longer holds instead of a dead-end retry."""
    review = dict(delivery.get("review") or {})
    review["status"] = "stale"
    review["detail"] = ("The reviewed files changed after the review. Run "
                        "Review again to see the current diff.")
    delivery["review"] = review
    delivery["status"] = "needs_review"
    delivery["current"] = {"head": snap["head"], "branch": snap["branch"],
                           "changed_paths": snap["paths"],
                           "fingerprint": snap["fingerprint"]}


def _test(mission):
    delivery, _baseline, repo, snap = _state(mission)
    review = delivery.get("review") or {}
    if review.get("status") != "reviewed":
        raise DeliveryError("review changes before running tests")
    reviewed = _reviewed_paths(review)
    gate = _fingerprint(repo, snap["head"], snap["branch"], reviewed)
    if review.get("fingerprint") != gate:
        _mark_review_stale(delivery, snap)
        raise DeliveryError(
            "the reviewed files changed after review; review them again")
    try:
        override = _override_test_argv(repo)
        argv, test_cwd = override if override else (detect_test_argv(repo), repo)
    except DeliveryError as exc:
        detail = str(exc)[:500]
        delivery["tests"] = {"status": "unavailable", "checked_at": _now(),
                             "fingerprint": snap["fingerprint"],
                             "head": snap["head"], "detail": detail,
                             "error": detail}
        if delivery.get("changed") is False:
            delivery["commit"] = {"status": "not_needed"}
            delivery["push"] = {"status": "not_needed"}
        else:
            blocked = (delivery.get("commit") or {}).get("blocked_reason", "")
            delivery["commit"] = {"status": "pending",
                                  "blocked_reason": blocked}
            delivery["push"] = {"status": "pending"}
        delivery["status"] = "tests_unavailable"
        delivery["current"] = {
            "head": snap["head"], "branch": snap["branch"],
            "changed_paths": snap["paths"],
            "fingerprint": snap["fingerprint"],
        }
        raise
    # Python's import cache must not manufacture untracked files between the
    # review and commit gates. Mutations to the REVIEWED paths are a failed
    # gate: that generated content was never approved for commit. Unrelated
    # paths (logs, caches) never enter the commit set, so they cannot fail it.
    python_runner = (argv[0] == sys.executable or
                     (len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]))
    test_env = {"PYTHONDONTWRITEBYTECODE": "1"} if python_runner else None
    result = _run(argv, test_cwd, timeout=900, env=test_env)
    after = _snapshot(repo)
    after_gate = _fingerprint(repo, after["head"], after["branch"], reviewed)
    mutated = after_gate != gate
    output = ((result.get("stdout") or "") +
              (("\n" + result.get("stderr", "")) if result.get("stderr") else ""))
    if mutated:
        output += ("\nTest command changed the reviewed files. Review the generated "
                   "changes before continuing; they were not approved for commit.")
    if (result["returncode"] and argv[0] == sys.executable and "--" in argv and
            "ModuleNotFoundError" in output):
        output += ("\nHint: these tests ran under Rune's own Python because the "
                   "project's environment was not found. Create the project's "
                   ".venv (or Poetry env), or pin the command in .rune-test.json "
                   'at the repository root: {"argv": ["..."], "cwd": "optional/subdir"}.')
    tests = {"status": "passed" if not result["returncode"] and not mutated else "failed",
             "ran_at": _now(), "fingerprint": after_gate,
             "head": after["head"], "argv": list(argv),
             "returncode": result["returncode"],
             "worktree_mutated": mutated,
             "output": _redact(output, OUTPUT_LIMIT)}
    delivery["tests"] = tests
    if delivery.get("changed") is False:
        delivery["commit"] = {"status": "not_needed"}
        delivery["push"] = {"status": "not_needed"}
    else:
        blocked = (delivery.get("commit") or {}).get("blocked_reason", "")
        delivery["commit"] = {"status": "pending", "blocked_reason": blocked}
        delivery["push"] = {"status": "pending"}
    delivery["status"] = "tested" if tests["status"] == "passed" else "tests_failed"
    delivery["current"] = {"head": after["head"], "branch": after["branch"],
                           "changed_paths": after["paths"],
                           "fingerprint": after["fingerprint"]}
    return {"delivery": public_delivery(delivery)}


def _commit_message(mission, message):
    message = str(message or "").strip() or "Rune: %s" % str(
        mission.get("name") or "complete mission")
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    message = re.sub(r"\s+", " ", message).strip()[:120]
    if not message:
        raise DeliveryError("commit message is empty")
    return message


def _commit(mission, message, peer_active=False):
    delivery, baseline, repo, snap = _state(mission)
    if peer_active:
        raise DeliveryError("another active mission shares this repository")
    blocked = _blocked_reason(baseline)
    if blocked:
        delivery.setdefault("commit", {})["blocked_reason"] = blocked
        raise DeliveryError(blocked)
    review, tests = delivery.get("review") or {}, delivery.get("tests") or {}
    if review.get("status") != "reviewed" or tests.get("status") != "passed":
        raise DeliveryError("review and passing tests are required before commit")
    reviewed = _reviewed_paths(review)
    gate = _fingerprint(repo, snap["head"], snap["branch"], reviewed)
    if (review.get("fingerprint") != gate or tests.get("fingerprint") != gate):
        _mark_review_stale(delivery, snap)
        raise DeliveryError(
            "the reviewed files changed after review/tests; run them again")
    # A worker may have committed its own clean result. Record that commit rather
    # than creating an empty one, but only when the mission began clean.
    if not reviewed and snap["head"] != baseline.get("head"):
        delivery["commit"] = {"status": "committed", "head": snap["head"],
                              "short_head": snap["head"][:10],
                              "committed_at": _now(), "existing": True,
                              "input_fingerprint": gate}
        delivery["push"] = {"status": "pending"}
        delivery["status"] = "committed"
        return {"delivery": public_delivery(delivery)}
    if snap["head"] != baseline.get("head"):
        raise DeliveryError("Git HEAD changed during the mission while changes remain dirty")
    # Commit exactly the reviewed, mission-attributed paths: never files that
    # appeared after review, and never paths that were already dirty before
    # the mission started (that is the operator's own work in progress).
    preexisting = set(baseline.get("dirty_paths") or [])
    paths = [path for path in reviewed if path not in preexisting]
    if not paths:
        reason = ("every reviewed change overlaps files that were already dirty "
                  "before the mission started; commit the operator's work "
                  "manually in Git" if reviewed else
                  "there are no attributable changes to commit")
        delivery.setdefault("commit", {})["blocked_reason"] = reason
        raise DeliveryError(reason)
    add = _git(repo, "add", "-A", "--", *paths, timeout=120)
    if add["returncode"]:
        raise DeliveryError("git add failed: %s" %
                            (add.get("stderr") or add.get("stdout") or "unknown error")[:1200])
    staged_raw = _require_raw(_git(repo, "diff", "--cached", "--name-only", "-z"),
                              "staged-path check")
    staged = sorted(set(_clean_path(path) for path in staged_raw.split("\x00") if _clean_path(path)))
    if not staged or not set(staged).issubset(set(paths)):
        raise DeliveryError("staged changes escaped the attributed mission path set")
    check = _git(repo, "diff", "--cached", "--check", timeout=120)
    if check["returncode"]:
        raise DeliveryError("staged diff check failed: %s" %
                            (check.get("stdout") or check.get("stderr") or "invalid diff")[:1200])
    commit_message = _commit_message(mission, message)
    result = _git(repo, "commit", "--only", "-m", commit_message, "--", *paths,
                  timeout=600)
    if result["returncode"]:
        error = _redact(result.get("stderr") or result.get("stdout"), 2000)
        delivery["commit"] = {"status": "failed", "failed_at": _now(),
                              "error": error}
        delivery["status"] = "commit_failed"
        raise DeliveryError("git commit failed: %s" %
                            (error or "the repository rejected the commit"))
    new_snap = _snapshot(repo)
    if not new_snap["head"] or new_snap["head"] == snap["head"]:
        raise DeliveryError("git commit did not create a new HEAD")
    delivery["commit"] = {"status": "committed", "head": new_snap["head"],
                          "short_head": new_snap["head"][:10],
                          "committed_at": _now(), "message": commit_message,
                          "paths": staged, "input_fingerprint": snap["fingerprint"]}
    excluded = sorted(set(reviewed) & preexisting)
    if excluded:
        delivery["commit"]["excluded_preexisting"] = excluded
    delivery["push"] = {"status": "pending"}
    delivery["status"] = "committed"
    delivery["current"] = {"head": new_snap["head"], "branch": new_snap["branch"],
                           "changed_paths": new_snap["paths"],
                           "fingerprint": new_snap["fingerprint"]}
    return {"delivery": public_delivery(delivery)}


def _push_target(repo, branch):
    if not branch:
        raise DeliveryError("cannot push a detached HEAD")
    valid = _git(repo, "check-ref-format", "--branch", branch)
    if valid["returncode"]:
        raise DeliveryError("current branch name is not pushable")
    remotes = [line.strip() for line in _require(_git(repo, "remote"), "Git remotes").splitlines()
               if line.strip()]
    if not remotes:
        raise DeliveryError("no Git remote is configured")
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                    "@{upstream}")
    if not upstream["returncode"]:
        value = (upstream.get("stdout") or "").strip()
        remote, separator, remote_branch = value.partition("/")
        if separator and remote in remotes and remote_branch:
            return remote, remote_branch, False
    remote = "origin" if "origin" in remotes else (remotes[0] if len(remotes) == 1 else "")
    if not remote:
        raise DeliveryError("branch has no upstream and no unique default remote")
    return remote, branch, True


def _prepare_push(mission):
    delivery, _baseline, repo, snap = _state(mission)
    commit = delivery.get("commit") or {}
    tests = delivery.get("tests") or {}
    if commit.get("status") != "committed" or tests.get("status") != "passed":
        raise DeliveryError("a verified commit is required before push")
    if snap["head"] != commit.get("head"):
        raise DeliveryError("Git HEAD changed after commit")
    # Push publishes commits only; unrelated local churn cannot alter them.
    dirty = sorted(set(commit.get("paths") or []) & set(snap["paths"]))
    if dirty:
        raise DeliveryError("committed files changed again before push: " +
                            ", ".join(dirty[:5]))
    remote, remote_branch, set_upstream = _push_target(repo, snap["branch"])
    token = secrets.token_urlsafe(24)
    expires = int(time.time()) + PUSH_TOKEN_TTL
    delivery["push"] = {"status": "awaiting_confirmation",
                        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": expires, "head": snap["head"],
                        "remote": remote, "branch": remote_branch,
                        "local_branch": snap["branch"],
                        "set_upstream": set_upstream}
    delivery["status"] = "push_ready"
    return {"delivery": public_delivery(delivery),
            "confirmation": {"token": token, "remote": remote,
                             "branch": remote_branch,
                             "head": snap["head"],
                             "short_head": snap["head"][:10],
                             "set_upstream": set_upstream}}


def _confirm_push(mission, token):
    delivery, _baseline, repo, snap = _state(mission)
    push = delivery.get("push") or {}
    expected = str(push.get("token_hash") or "")
    supplied = hashlib.sha256(str(token or "").encode()).hexdigest()
    # Consume before any outward action; replay is impossible even on failure.
    push.pop("token_hash", None)
    delivery["push"] = push
    if (push.get("status") != "awaiting_confirmation" or not expected or
            not secrets.compare_digest(expected, supplied)):
        delivery["push"] = dict(push, status="confirmation_invalid")
        delivery["status"] = "push_confirmation_required"
        raise DeliveryError("push confirmation is invalid or already used")
    if int(push.get("expires_at") or 0) < int(time.time()):
        delivery["push"] = dict(push, status="confirmation_expired")
        delivery["status"] = "push_confirmation_required"
        raise DeliveryError("push confirmation expired; prepare it again")
    commit = delivery.get("commit") or {}
    if snap["head"] != push.get("head") or snap["head"] != commit.get("head"):
        raise DeliveryError("Git HEAD changed after push confirmation")
    if snap["branch"] != push.get("local_branch"):
        raise DeliveryError("branch changed after push confirmation")
    if set(commit.get("paths") or []) & set(snap["paths"]):
        raise DeliveryError("committed files changed after push confirmation")
    remote, remote_branch, set_upstream = _push_target(repo, snap["branch"])
    if (remote != push.get("remote") or remote_branch != push.get("branch") or
            bool(set_upstream) != bool(push.get("set_upstream"))):
        raise DeliveryError("Git push target changed after confirmation")
    argv = ["git", "-C", repo, "push", "--porcelain"]
    if set_upstream:
        argv.append("--set-upstream")
    argv += [remote, "refs/heads/%s:refs/heads/%s" %
             (snap["branch"], remote_branch)]
    result = _run(argv, repo, timeout=300, env={"GIT_TERMINAL_PROMPT": "0"})
    if result["returncode"]:
        error = _redact(result.get("stderr") or result.get("stdout"), 2000)
        delivery["push"] = {"status": "failed", "failed_at": _now(),
                            "remote": remote, "branch": remote_branch,
                            "head": snap["head"],
                            "error": error}
        delivery["status"] = "push_failed"
        raise DeliveryError("git push failed: %s" %
                            (error or "the remote rejected the push"))
    delivery["push"] = {"status": "pushed", "pushed_at": _now(),
                        "remote": remote, "branch": remote_branch,
                        "head": snap["head"],
                        "output": _redact(result.get("stdout"), 2000)}
    delivery["status"] = "pushed"
    return {"delivery": public_delivery(delivery)}


def perform(mission, action, message="", token="", peer_active=False):
    """Mutate one server-loaded mission using an allowlisted delivery action."""
    action = str(action or "")
    if action == "review":
        return _review(mission)
    if action == "test":
        return _test(mission)
    if action == "commit":
        return _commit(mission, message, peer_active=peer_active)
    if action == "prepare_push":
        return _prepare_push(mission)
    if action == "confirm_push":
        return _confirm_push(mission, token)
    raise DeliveryError("unknown delivery action")


def public_delivery(value):
    """Deep-copy delivery state while removing confirmation secrets."""
    clean = json.loads(json.dumps(value if isinstance(value, dict) else {}))
    push = clean.get("push") if isinstance(clean.get("push"), dict) else {}
    push.pop("token_hash", None)
    clean["push"] = push
    return clean
