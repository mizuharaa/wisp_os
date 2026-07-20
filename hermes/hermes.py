#!/usr/bin/env python3
"""Hermes: a bounded, inspectable solved-problem memory.

Legacy commands remain compatible::

  python hermes/hermes.py query "TEXT"            # exit 0 hit, 1 miss
  python hermes/hermes.py note "PROBLEM" "SOLUTION" [--tags a,b] [--source S]
  python hermes/hermes.py stale ID
  python hermes/hermes.py list

Machine consumers can add ``--json`` to query/note or use ``stats --json``.
The public Python API is ``query_memory``, ``record_reuse``, ``note_memory``
and ``storage_health``. Query ranking is deliberately independent of historical
hit count so frequently recalled cards do not become a popularity feedback
loop.
"""
from __future__ import annotations

import contextlib
import collections
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOLVED = os.path.join(HERE, "solved.jsonl")
QUARANTINE = os.path.join(HERE, "quarantine.jsonl")
ARCHIVE_DIR = os.path.join(HERE, "archive")
ARCHIVE_INDEX = os.path.join(ARCHIVE_DIR, "index.json")
USAGE = os.path.join(HERE, "usage.json")

SCHEMA_VERSION = 2
QUALITY_VERSION = 1
HIT_AT = 0.34
QUALITY_AT = 0.46
DUPLICATE_AT = 0.78
MAX_PER_SOURCE = 2

DEFAULT_MAX_CARDS = 500
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_QUARANTINE_MAX_BYTES = 1024 * 1024
DEFAULT_ARCHIVE_MAX_BYTES = 16 * 1024 * 1024

sys.path.insert(0, os.path.join(ROOT, "memory"))
from pipeline import vault_path  # noqa: E402 - repository-local import


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _env_int(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _limits():
    return {
        "max_cards": _env_int("HERMES_MAX_CARDS", DEFAULT_MAX_CARDS),
        "max_bytes": _env_int("HERMES_MAX_BYTES", DEFAULT_MAX_BYTES, 1024),
        "quarantine_max_bytes": _env_int(
            "HERMES_QUARANTINE_MAX_BYTES", DEFAULT_QUARANTINE_MAX_BYTES, 512
        ),
        "archive_max_bytes": _env_int(
            "HERMES_ARCHIVE_MAX_BYTES", DEFAULT_ARCHIVE_MAX_BYTES, 1024
        ),
    }


def _load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON in %s line %d" % (path, number)) from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load():
    """Load active cards. Kept as a compatibility API for existing callers."""
    return _load_jsonl(SOLVED)


def _jsonl_bytes(rows):
    return sum(
        len((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        for row in rows
    )


def _atomic_write(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hermes-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(rows):
    """Atomically replace the active card file (legacy public API)."""
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    _atomic_write(SOLVED, text)


@contextlib.contextmanager
def _file_lock(path, timeout=4.0):
    """Small cross-process lock; the lock file is ephemeral and bounded."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    deadline = time.monotonic() + timeout
    acquired = False
    while not acquired:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write("%s %s\n" % (os.getpid(), time.time()))
            acquired = True
        except FileExistsError:
            try:
                stale = time.time() - os.path.getmtime(lock_path) > 30
            except OSError:
                stale = False
            if stale:
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("Hermes storage is busy")
            time.sleep(0.025)
    try:
        yield
    finally:
        if acquired:
            try:
                os.unlink(lock_path)
            except OSError:
                pass


STOPWORDS = {
    "and", "are", "but", "for", "from", "has", "have", "into", "its", "not",
    "that", "the", "their", "then", "this", "was", "were", "when", "with",
    "you", "your", "after", "before", "using", "use", "does", "each",
}

TOKEN_ALIASES = {
    "cached": "cache", "caches": "cache", "caching": "cache",
    "daily": "day", "days": "day",
    "events": "event",
    "fresh": "refresh", "refreshed": "refresh", "refreshes": "refresh",
    "plans": "plan",
    "retries": "retry", "retried": "retry",
    "tests": "test", "tested": "test", "testing": "test",
}


def tokens(text):
    found = set()
    for word in re.findall(r"[a-z0-9][a-z0-9_.-]+", str(text).lower()):
        pieces = [word] + re.split(r"[_.-]+", word)
        for piece in pieces:
            piece = TOKEN_ALIASES.get(piece, piece)
            if len(piece) > 2 and piece not in STOPWORDS:
                found.add(piece)
    return found


def _reason(code, weight, detail):
    return {"code": code, "weight": round(weight, 3), "detail": detail}


def evaluate_quality(problem, solution, tags=None, source=""):
    """Return a deterministic, explainable reusability score in ``[0, 1]``."""
    problem = str(problem or "").strip()
    solution = str(solution or "").strip()
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    tags = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    combined = (problem + "\n" + solution).lower()
    signals, penalties = [], []
    score = 0.10

    evidence = re.search(
        r"\b(reproduced|verified|validated|confirmed|traced|bisected|profiled|measured|"
        r"regression(?: test)?|tests? pass(?:ed|ing)?|assert(?:ed|ion)?)\b", combined
    )
    if evidence:
        signals.append(_reason("hard_earned_evidence", 0.18,
                               "Includes reproducible or verified evidence."))
        score += 0.18

    root_cause = re.search(
        r"\b(root cause|caus(?:e|es|ed|ing)|because|due to|originated from|triggered by|"
        r"therefore|leads? to|leaving|left .{0,40} for|which (?:means|causes)|"
        r"race condition|deadlock|order dependence|invariant)\b", combined
    )
    if root_cause:
        signals.append(_reason("root_cause", 0.20,
                               "Explains why the problem occurs, not only its status."))
        score += 0.20

    action_words = re.findall(
        r"\b(run|use|replace|set|add|remove|check|verify|configure|wrap|retry|pin|clear|"
        r"load|persist|compare|guard|isolate|move|call|create|raise|slice|report|"
        r"log|emit|pass|scan|sum|animate|score|compute|route|return|subclass|send|fetch)\b",
        solution.lower()
    )
    recipe = bool(action_words) or bool(re.search(
        r"\b(first|next|finally|step \d+|then (?:run|use|check|verify|replace|set|add))\b",
        solution.lower()
    ))
    if recipe:
        signals.append(_reason("reusable_recipe", 0.24,
                               "Contains an actionable procedure or implementation move."))
        score += 0.24

    mechanism = re.search(
        r"\b(blocks?|allows?|inherits?|fires? only|converges?|counts? against|"
        r"routes?|resolves?|reads?|writes?|intercepts?|reconstruct(?:s|ed)?|"
        r"is a no-op|single choke point|window start|reset =)\b", solution.lower()
    )
    if mechanism:
        signals.append(_reason("technical_mechanism", 0.16,
                               "States a concrete mechanism or behavioral invariant."))
        score += 0.16

    gotcha = re.search(
        r"\b(gotcha|avoid|must|never|otherwise|instead|watch for|caveat|"
        r"race condition|deadlock|order dependence|silently|only after)\b", combined
    )
    if gotcha:
        signals.append(_reason("gotcha_or_guardrail", 0.12,
                               "Captures a failure mode or guardrail worth reusing."))
        score += 0.12

    reusable_tags = {
        "reusable", "pattern", "testing", "debugging", "architecture", "windows",
        "linux", "python", "javascript", "api", "database", "cache", "security",
        "race-condition", "recovery", "orchestration", "oauth", "ssh", "hooks",
        "claude-code", "git", "cli", "frontend", "backend", "polling",
    }
    cross_project = bool(set(tags) & reusable_tags) or bool(re.search(
        r"\b(for any|across (?:projects|repositories|services)|general pattern|"
        r"whenever|when .{3,80}, (?:use|avoid|check|set)|portable|cross-project)\b",
        combined
    ))
    if cross_project:
        signals.append(_reason("cross_project_potential", 0.12,
                               "Names a portable pattern or broadly reusable domain."))
        score += 0.12

    if len(problem) >= 24 and 55 <= len(solution) <= 1800:
        signals.append(_reason("specific_and_compact", 0.08,
                               "Has enough detail to act on without being a raw dump."))
        score += 0.08

    if str(source or "").strip() and str(source).strip() not in {"?", "unknown"}:
        signals.append(_reason("source_provenance", 0.04,
                               "Includes source provenance for later review."))
        score += 0.04

    raw_markers = len(re.findall(
        r"(?m)^(?:\[?\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}[ t]|"
        r"(?:debug|info|warn|error|trace)\b|traceback \(most recent call last\))",
        combined
    ))
    jsonish_lines = len(re.findall(r"(?m)^\s*[\[{\]}][,\s]*$|^\s*\"[^\"]+\"\s*:", solution))
    if raw_markers >= 2 or jsonish_lines >= 5:
        penalties.append(_reason("raw_log_or_dump", -0.35,
                                 "Looks like raw logs/JSON rather than a distilled lesson."))
        score -= 0.35

    one_off = re.search(
        r"\b(completed task|task complete|done now|finished today|current status|"
        r"mission accepted|accepted after|shipped today|weekly update|assignment complete|"
        r"status:\s*(?:complete|done)|[a-z][a-z0-9_-]+=done)\b", combined
    )
    if one_off and not (root_cause and recipe):
        penalties.append(_reason("one_off_status", -0.25,
                                 "Describes one run's status without a reusable explanation."))
        score -= 0.25

    failure = re.search(r"\b(fail(?:ed|ure|ing)?|error|exception|broken|timeout)\b", problem.lower())
    resolution = recipe or root_cause or re.search(
        r"\b(fix(?:ed)?|resolved|prevent(?:ed|s)?|restore(?:d)?|workaround|passes|"
        r"changed|corrected|introduced|switched|updated|refactored|handled)\b",
        solution.lower()
    )
    if failure and not resolution:
        penalties.append(_reason("unresolved_failure", -0.28,
                                 "Mentions failure without a reusable resolution."))
        score -= 0.28

    combined_length = len(problem) + len(solution)
    if combined_length > 3500:
        penalties.append(_reason("huge_dump", -0.35,
                                 "Exceeds the useful card size; distillation is required."))
        score -= 0.35
    elif combined_length > 1800:
        penalties.append(_reason("oversized", -0.18,
                                 "Long enough to obscure the reusable core."))
        score -= 0.18

    if len(solution) < 45:
        penalties.append(_reason("vague_solution", -0.30,
                                 "Solution is too short to reproduce safely."))
        score -= 0.30
    if len(problem) < 15:
        penalties.append(_reason("vague_problem", -0.12,
                                 "Problem statement lacks a discriminating condition."))
        score -= 0.12

    score = round(min(1.0, max(0.0, score)), 3)
    return {
        "version": QUALITY_VERSION,
        "score": score,
        "threshold": QUALITY_AT,
        "signals": signals,
        "penalties": penalties,
        "decision": "accepted" if score >= QUALITY_AT else "quarantined",
    }


def _normal_tags(tags):
    if isinstance(tags, str):
        tags = tags.split(",")
    return list(dict.fromkeys(
        str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()
    ))


def _similarity(left, right):
    lp, rp = tokens(left.get("problem", "")), tokens(right.get("problem", ""))
    ls, rs = tokens(left.get("solution", "")), tokens(right.get("solution", ""))

    def jaccard(a, b):
        return len(a & b) / len(a | b) if a or b else 0.0

    problem_score = jaccard(lp, rp)
    solution_score = jaccard(ls, rs)
    combined_score = jaccard(lp | ls, rp | rs)
    return round(max(problem_score, 0.65 * problem_score + 0.25 * solution_score
                     + 0.10 * combined_score), 3)


def _find_duplicate(rows, candidate):
    matches = [(_similarity(row, candidate), row) for row in rows]
    matches = [(score, row) for score, row in matches if score >= DUPLICATE_AT]
    if not matches:
        return None, 0.0
    matches.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))
    return matches[0][1], matches[0][0]


def brain_folder(create=True):
    vp = vault_path()
    if not vp or not os.path.isdir(vp):
        return None
    folder = os.path.join(vp, "Rune", "Hermes")
    if create:
        os.makedirs(folder, exist_ok=True)
    elif not os.path.isdir(folder):
        return None
    return folder


def _card_filename(row):
    """Return a traversal-safe filename for one Hermes-owned vault mirror."""
    rid = re.sub(r"[^a-zA-Z0-9_-]", "", str(row.get("id", "")))[:64]
    if not rid:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(row.get("problem", "")).lower()).strip("-")[:60]
    return "%s-%s.md" % (rid, slug or "memory")


def card_path(row, create=True):
    folder = brain_folder(create=create)
    if not folder:
        return None
    filename = _card_filename(row)
    return os.path.join(folder, filename) if filename else None


def _empty_mirror_cleanup():
    return {"removed_count": 0, "removed_bytes": 0, "missing_count": 0,
            "failed_count": 0}


def _remove_archived_card_mirrors(rows):
    """Remove only exact Hermes card mirrors whose rows are already archived.

    The caller invokes this only after the gzip archive append and active JSONL
    replacement both succeed. No glob or prefix deletion is used.
    """
    report = _empty_mirror_cleanup()
    folder = brain_folder(create=False)
    if not folder:
        report["missing_count"] = len(rows)
        return report
    folder_abs = os.path.abspath(folder)
    for row in rows:
        filename = _card_filename(row)
        if not filename:
            report["failed_count"] += 1
            continue
        path = os.path.abspath(os.path.join(folder_abs, filename))
        try:
            if os.path.commonpath([folder_abs, path]) != folder_abs:
                report["failed_count"] += 1
                continue
        except ValueError:
            report["failed_count"] += 1
            continue
        if not os.path.lexists(path):
            report["missing_count"] += 1
            continue
        try:
            size = os.lstat(path).st_size
            if os.path.isdir(path) and not os.path.islink(path):
                report["failed_count"] += 1
                continue
            os.unlink(path)
            report["removed_count"] += 1
            report["removed_bytes"] += size
        except OSError:
            report["failed_count"] += 1
    return report


def _sync_vault_after_save(active_row, active_rows, archived_rows):
    """Mirror the active card, prune exact archived mirrors, then refresh MOC."""
    report = _empty_mirror_cleanup()
    if active_row is not None:
        try:
            write_card(active_row)
        except OSError:
            report["failed_count"] += 1
    cleanup = _remove_archived_card_mirrors(archived_rows)
    for key in report:
        report[key] += cleanup[key]
    try:
        write_index(active_rows)
    except OSError:
        report["failed_count"] += 1
    return report


def write_index(rows):
    """Regenerate the browsable MOC for active cards only."""
    folder = brain_folder()
    if not folder:
        return
    stale_n = sum(1 for row in rows if row.get("stale"))
    lines = [
        "---", "generated: " + _now(), "---", "",
        "# Rune · Hermes — reusable solutions", "",
        "%d active · %d stale. Low-value writes stay out of the active brain."
        % (len(rows), stale_n), "",
    ]
    for row in sorted(rows, key=lambda item: item.get("last_verified", item.get("ts", "")),
                      reverse=True):
        path = card_path(row)
        if not path:
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        flag = " ⚠ STALE" if row.get("stale") else ""
        tag_text = " ".join("#" + tag for tag in row.get("tags", []))
        lines.append("- [[%s|%s]] — %s %s%s" % (
            name, row["problem"], (row.get("last_verified") or row.get("ts") or "")[:10],
            tag_text, flag
        ))
    _atomic_write(os.path.join(folder, "_index.md"), "\n".join(lines) + "\n")


def write_card(row):
    path = card_path(row)
    if not path:
        return None
    quality = row.get("quality") or evaluate_quality(
        row.get("problem", ""), row.get("solution", ""), row.get("tags", []),
        row.get("source", "")
    )
    content = (
        "---\nid: %s\ndate: %s\nlast_verified: %s\ntags: [%s]\nsource: %s\n"
        "quality: %.3f\nreinforcements: %d\nstale: %s\n---\n\n# %s\n\n"
        "**Reusable solution:**\n\n%s\n"
        % (
            row["id"], row.get("first_seen", row.get("ts", "")),
            row.get("last_verified", row.get("ts", "")),
            ", ".join(row.get("tags", [])), row.get("source", "?"),
            quality.get("score", 0.0), int(row.get("reinforcement_count", 1)),
            str(row.get("stale", False)).lower(), row["problem"], row["solution"],
        )
    )
    _atomic_write(path, content)
    return path


def _archive_total_bytes():
    if not os.path.isdir(ARCHIVE_DIR):
        return 0
    return sum(
        os.path.getsize(os.path.join(ARCHIVE_DIR, name))
        for name in os.listdir(ARCHIVE_DIR)
        if name.endswith(".gz") and os.path.isfile(os.path.join(ARCHIVE_DIR, name))
    )


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _archive_index():
    base = {
        "schema_version": SCHEMA_VERSION,
        "archived_count": 0,
        "solved_count": 0,
        "quarantine_count": 0,
        "last_archived_at": None,
    }
    base.update(_load_json(ARCHIVE_INDEX, {}))
    return base


def _append_archive(rows, channel, reason):
    """Losslessly gzip rows without crossing the configured hard byte cap."""
    if not rows:
        return True
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(ARCHIVE_DIR, channel + ".jsonl.gz")
    archived_at = _now()
    envelopes = []
    for row in rows:
        envelopes.append(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "archived_at": archived_at,
            "archive_reason": reason,
            "channel": channel,
            "row": row,
        }, ensure_ascii=False) + "\n")
    # Each append is a valid concatenated gzip member. Pre-compression gives us
    # an exact projected size instead of a cap check that can overshoot.
    compressed = gzip.compress("".join(envelopes).encode("utf-8"), compresslevel=9, mtime=0)
    with _file_lock(os.path.join(ARCHIVE_DIR, "archive")):
        maximum = _limits()["archive_max_bytes"]
        if _archive_total_bytes() + len(compressed) > maximum:
            return False
        with open(path, "ab") as handle:
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        index = _archive_index()
        count = len(rows)
        index["archived_count"] = int(index.get("archived_count", 0)) + count
        key = "quarantine_count" if channel == "quarantine" else "solved_count"
        index[key] = int(index.get(key, 0)) + count
        index["last_archived_at"] = archived_at
        _atomic_write(ARCHIVE_INDEX,
                      json.dumps(index, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def _usage_state():
    base = {
        "schema_version": SCHEMA_VERSION,
        "queries": 0,
        "hit_queries": 0,
        "miss_queries": 0,
        "last_query_at": None,
        "cards": {},
        "writes": {"accepted": 0, "merged": 0, "quarantined": 0, "rejected": 0},
    }
    loaded = _load_json(USAGE, {})
    base.update(loaded)
    if not isinstance(base.get("cards"), dict):
        base["cards"] = {}
    if not isinstance(base.get("writes"), dict):
        base["writes"] = {}
    for key in ("accepted", "merged", "quarantined", "rejected"):
        base["writes"][key] = int(base["writes"].get(key, 0))
    return base


def _write_usage(state):
    _atomic_write(USAGE, json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n")


def _record_write_outcome(outcome):
    try:
        with _file_lock(USAGE):
            state = _usage_state()
            state["writes"][outcome] = int(state["writes"].get(outcome, 0)) + 1
            _write_usage(state)
    except (OSError, TimeoutError, ValueError):
        pass


def _record_query(hit_ids, active_ids):
    """Persist bounded usage metadata without rewriting the knowledge corpus."""
    try:
        with _file_lock(USAGE):
            state = _usage_state()
            stamp = _now()
            state["queries"] = int(state.get("queries", 0)) + 1
            outcome_key = "hit_queries" if hit_ids else "miss_queries"
            state[outcome_key] = int(state.get(outcome_key, 0)) + 1
            state["last_query_at"] = stamp
            cards = state["cards"]
            for rid in hit_ids:
                item = cards.get(rid) if isinstance(cards.get(rid), dict) else {}
                item["hit_count"] = int(item.get("hit_count", 0)) + 1
                item["last_hit_at"] = stamp
                cards[rid] = item
            # Bounded by active cards; archived IDs cannot leave an SSD tail.
            state["cards"] = {rid: cards[rid] for rid in sorted(cards) if rid in active_ids}
            _write_usage(state)
        return True
    except (OSError, TimeoutError, ValueError):
        return False


def record_reuse(hit_ids, active_ids=None):
    """Record only hits actually exposed to a model, without reranking.

    Callers that preview with ``query_memory(..., record_hits=False)`` should
    invoke this at the context-injection boundary. This keeps early stops and
    abandoned plans from being counted as knowledge reuse.
    """
    hit_ids = list(dict.fromkeys(str(rid) for rid in (hit_ids or []) if str(rid)))
    if active_ids is None:
        active_ids = {str(row.get("id", "")) for row in load()}
    else:
        active_ids = {str(rid) for rid in active_ids}
    hit_ids = [rid for rid in hit_ids if rid in active_ids]
    return _record_query(hit_ids, active_ids)


def _quarantine_receipt(problem, solution, tags, source, quality, stamp):
    raw = (problem + "\0" + solution).encode("utf-8", "replace")
    return {
        "schema_version": SCHEMA_VERSION,
        "id": hashlib.sha1(raw).hexdigest()[:12],
        "ts": stamp,
        "problem_excerpt": problem[:500],
        "solution_excerpt": solution[:1400],
        "tags": tags[:20],
        "source": source,
        "quality": quality,
        "reason_code": "quality_below_threshold",
        "original_sha256": hashlib.sha256(raw).hexdigest(),
        "original_bytes": len(raw),
        "truncated": len(problem) > 500 or len(solution) > 1400,
    }


def _store_quarantine(receipt):
    """Store a bounded review receipt, rotating losslessly to compressed archive."""
    maximum = _limits()["quarantine_max_bytes"]
    encoded = (json.dumps(receipt, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with _file_lock(QUARANTINE):
            current = _load_jsonl(QUARANTINE)
            current_bytes = _jsonl_bytes(current)
            if current and current_bytes + len(encoded) > maximum:
                if not _append_archive(current, "quarantine", "quarantine_budget_rotation"):
                    return False
                _atomic_write(QUARANTINE, "")
                current = []
            if len(encoded) > maximum:
                return False
            current.append(receipt)
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in current)
            _atomic_write(QUARANTINE, text)
        return True
    except (OSError, TimeoutError, ValueError):
        return False


def _row_priority(row):
    quality = row.get("quality")
    if not isinstance(quality, dict):
        quality = evaluate_quality(row.get("problem", ""), row.get("solution", ""),
                                   row.get("tags", []), row.get("source", ""))
    stamp = row.get("last_verified") or row.get("ts") or ""
    return (
        0 if row.get("stale") else 1,
        float(quality.get("score", 0.0)),
        min(20, int(row.get("reinforcement_count", 1))),
        stamp,
        str(row.get("id", "")),
    )


def _within_budget(rows):
    limits = _limits()
    return len(rows) <= limits["max_cards"] and _jsonl_bytes(rows) <= limits["max_bytes"]


def _compact(rows, protected_ids=()):
    """Return ``(kept, archived, ok)``; archival happens before active removal."""
    if _within_budget(rows):
        return rows, [], True
    protected = set(protected_ids)
    kept = list(rows)
    candidates = sorted(
        (row for row in kept if row.get("id") not in protected), key=_row_priority
    )
    archived = []
    while not _within_budget(kept) and candidates:
        victim = candidates.pop(0)
        kept.remove(victim)
        archived.append(victim)
    if not _within_budget(kept):
        return rows, [], False
    if not _append_archive(archived, "solved", "active_storage_budget"):
        return rows, [], False
    return kept, archived, True


def _quality_for_row(row):
    quality = row.get("quality")
    if isinstance(quality, dict) and "score" in quality:
        return quality
    return evaluate_quality(row.get("problem", ""), row.get("solution", ""),
                            row.get("tags", []), row.get("source", ""))


def _safe_sources(row):
    sources = row.get("sources") if isinstance(row.get("sources"), list) else []
    sources = [str(item) for item in sources if str(item).strip()]
    source = str(row.get("source") or "?")
    return list(dict.fromkeys([source] + sources))[:8]


def _note_result(outcome, rid, reason_code, quality, duplicate=None, archived=0,
                 mirror_cleanup=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "hermes.note",
        "outcome": outcome,
        "id": rid,
        "reason_code": reason_code,
        "quality": quality,
        "duplicate": duplicate,
        "archived_during_compaction": archived,
        "vault_cleanup": mirror_cleanup or _empty_mirror_cleanup(),
        "storage": storage_health(),
    }


def note_memory(problem, solution, tags="", source="conductor", now=None):
    """Store, reinforce, quarantine, or reject one candidate memory.

    Low-quality candidates return a successful, machine-readable outcome but do
    not enter the active corpus. This keeps existing best-effort callers from
    turning a memory policy decision into a mission failure.
    """
    problem = str(problem or "").strip()
    solution = str(solution or "").strip()
    tag_list = _normal_tags(tags)
    source = str(source or "conductor").strip() or "conductor"
    stamp = str(now or _now())
    quality = evaluate_quality(problem, solution, tag_list, source)
    candidate = {"problem": problem, "solution": solution}

    try:
        with _file_lock(SOLVED):
            rows = load()
            duplicate, similarity = _find_duplicate(rows, candidate)
            if duplicate is not None:
                duplicate_ref = {"id": duplicate.get("id"), "similarity": similarity}
                # Similar wording alone is not verification. Keep junk out of
                # retention priority and retain only a bounded audit receipt.
                if quality["score"] < QUALITY_AT:
                    receipt = _quarantine_receipt(
                        problem, solution, tag_list, source, quality, stamp
                    )
                    receipt["reason_code"] = "low_quality_duplicate"
                    receipt["duplicate"] = duplicate_ref
                    stored = _store_quarantine(receipt)
                    outcome = "quarantined" if stored else "rejected"
                    reason = "low_quality_duplicate" if stored else "quarantine_budget_exhausted"
                    _record_write_outcome(outcome)
                    return _note_result(outcome, receipt["id"], reason, quality,
                                        duplicate_ref)
                original_rows = [dict(row) for row in rows]
                existing_quality = _quality_for_row(duplicate)
                duplicate["first_seen"] = duplicate.get("first_seen", duplicate.get("ts", stamp))
                duplicate["last_seen"] = stamp
                duplicate["updated_at"] = stamp
                duplicate["reinforcement_count"] = int(duplicate.get("reinforcement_count", 1)) + 1
                duplicate["tags"] = list(dict.fromkeys(
                    list(duplicate.get("tags", [])) + tag_list
                ))[:24]
                sources = list(dict.fromkeys(_safe_sources(duplicate) + [source]))
                duplicate["sources"] = sources[-8:]
                if quality["score"] >= QUALITY_AT:
                    duplicate["last_verified"] = stamp
                    duplicate["stale"] = False
                    if quality["score"] > float(existing_quality.get("score", 0.0)) + 0.04:
                        duplicate["solution"] = solution
                        duplicate["quality"] = quality
                    else:
                        duplicate["quality"] = existing_quality
                else:
                    duplicate["quality"] = existing_quality
                compacted, archived, ok = _compact(rows, {duplicate.get("id")})
                if not ok:
                    # Do not grow active or archive storage for metadata alone.
                    rows = original_rows
                    _record_write_outcome("rejected")
                    return _note_result(
                        "rejected", duplicate.get("id"), "storage_budget_exhausted",
                        quality, duplicate_ref, 0
                    )
                save(compacted)
                mirror_cleanup = _sync_vault_after_save(duplicate, compacted, archived)
                _record_write_outcome("merged")
                return _note_result(
                    "merged", duplicate.get("id"), "near_duplicate_reinforced", quality,
                    duplicate_ref, len(archived), mirror_cleanup
                )

            if quality["score"] < QUALITY_AT:
                receipt = _quarantine_receipt(
                    problem, solution, tag_list, source, quality, stamp
                )
                stored = _store_quarantine(receipt)
                outcome = "quarantined" if stored else "rejected"
                reason = "quality_below_threshold" if stored else "quarantine_budget_exhausted"
                _record_write_outcome(outcome)
                return _note_result(outcome, receipt["id"], reason, quality)

            rid = hashlib.sha1((problem + solution).encode("utf-8")).hexdigest()[:7]
            existing_ids = {str(row.get("id")) for row in rows}
            if rid in existing_ids:
                rid = hashlib.sha1((problem + solution + stamp).encode("utf-8")).hexdigest()[:12]
            row = {
                "schema_version": SCHEMA_VERSION,
                "id": rid,
                "ts": stamp,
                "first_seen": stamp,
                "last_seen": stamp,
                "last_verified": stamp,
                "updated_at": stamp,
                "problem": problem,
                "solution": solution,
                "tags": tag_list,
                "source": source,
                "sources": [source],
                "stale": False,
                "reinforcement_count": 1,
                "quality": quality,
            }
            proposed = rows + [row]
            compacted, archived, ok = _compact(proposed, {rid})
            if not ok:
                receipt = _quarantine_receipt(problem, solution, tag_list, source, quality, stamp)
                receipt["reason_code"] = "storage_budget_exhausted"
                stored = _store_quarantine(receipt)
                outcome = "quarantined" if stored else "rejected"
                reason = "storage_budget_exhausted" if stored else "all_storage_budgets_exhausted"
                _record_write_outcome(outcome)
                return _note_result(outcome, receipt["id"], reason, quality)
            save(compacted)
            mirror_cleanup = _sync_vault_after_save(row, compacted, archived)
            _record_write_outcome("accepted")
            return _note_result("accepted", rid, "quality_threshold_met", quality,
                                archived=len(archived), mirror_cleanup=mirror_cleanup)
    except (OSError, TimeoutError, ValueError) as exc:
        # Explicit rejection is safer than reporting a note that never landed.
        quality = dict(quality)
        quality["storage_error"] = type(exc).__name__
        _record_write_outcome("rejected")
        return _note_result("rejected", None, "storage_unavailable", quality)


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _freshness(row):
    if row.get("stale"):
        return 0.5
    stamp = _parse_time(row.get("last_verified") or row.get("ts"))
    if not stamp:
        return 0.92
    age = max(0, (datetime.datetime.now(datetime.timezone.utc) - stamp).days)
    if age <= 180:
        return 1.0
    if age <= 730:
        return 0.96
    return 0.90


def _source_family(source):
    value = str(source or "?").strip().lower()
    return value.split(":", 1)[0] or "?"


def _corpus_fingerprint(rows):
    canonical = []
    for row in rows:
        canonical.append({
            "id": row.get("id"),
            "problem": row.get("problem", ""),
            "solution": row.get("solution", ""),
            "tags": sorted(row.get("tags", [])),
            "source": row.get("source", "?"),
            "sources": sorted(_safe_sources(row)),
            "stale": bool(row.get("stale")),
            "last_verified": row.get("last_verified", row.get("ts")),
        })
    canonical.sort(key=lambda item: str(item.get("id", "")))
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _idf_weights(rows):
    frequencies = collections.Counter()
    for row in rows:
        document = (
            tokens(row.get("problem", "")) | tokens(row.get("solution", "")) |
            tokens(" ".join(row.get("tags", [])))
        )
        frequencies.update(document)
    total = max(1, len(rows))
    return {
        term: math.log((total + 1.0) / (count + 0.5)) + 1.0
        for term, count in frequencies.items()
    }


def _candidate_score(query_tokens, row, idf=None):
    problem_tokens = tokens(row.get("problem", ""))
    solution_tokens = tokens(row.get("solution", ""))
    tag_tokens = tokens(" ".join(row.get("tags", [])))
    document = problem_tokens | solution_tokens | tag_tokens
    matched = query_tokens & document
    if not matched:
        return None
    # Longer natural-language queries require several corroborating concepts;
    # two generic matches must not turn a calendar-layout query into an auth hit.
    required_matches = 1 if len(query_tokens) <= 2 else (2 if len(query_tokens) <= 4 else 3)
    if len(matched) < required_matches:
        return None
    idf = idf or {}
    weight = lambda term: float(idf.get(term, 1.0))
    query_weight = sum(weight(term) for term in query_tokens) or 1.0
    document_coverage = sum(weight(term) for term in matched) / query_weight
    problem_matches = query_tokens & problem_tokens
    problem_coverage = sum(weight(term) for term in problem_matches) / query_weight
    # Cosine focus penalizes giant mission prompts that happen to contain a few
    # generic query words, without punishing concise root-cause cards.
    focus = len(problem_matches) / math.sqrt(
        max(1, len(query_tokens)) * max(1, len(problem_tokens))
    )
    lexical = min(1.0, 0.35 * problem_coverage + 0.35 * document_coverage + 0.30 * focus)
    quality = _quality_for_row(row)
    freshness = _freshness(row)
    # Relevance remains the gate and majority weight; quality then promotes a
    # distilled recipe above a copied mission prompt with the same keywords.
    score = round((0.76 * lexical + 0.24 * float(quality.get("score", 0.0)))
                  * freshness, 4)
    reasons = ["matched_terms:" + ",".join(sorted(matched)[:8])]
    reasons.extend("quality:" + item["code"] for item in quality.get("signals", [])[:3])
    if row.get("stale"):
        reasons.append("freshness:stale_penalty")
    return {
        "row": row,
        "score": score,
        "score_components": {
            "lexical": round(lexical, 4),
            "quality": float(quality.get("score", 0.0)),
            "freshness": round(freshness, 3),
        },
        "reasons": reasons,
    }


def query_memory(text, limit=3, record_hits=True):
    """Rank reusable cards once, with provenance and an auditable score receipt."""
    text = str(text or "").strip()
    try:
        limit = min(20, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 3
    rows = load()
    query_tokens = tokens(text)
    fingerprint = _corpus_fingerprint(rows)
    usage = _usage_state()
    base = {
        "schema_version": SCHEMA_VERSION,
        "kind": "hermes.query",
        "query": text,
        "decision": "miss",
        "threshold": HIT_AT,
        "evaluated_count": len(rows),
        "corpus_count": len(rows),
        "corpus_fingerprint": fingerprint,
        "hits": [],
        "guards": {
            "max_per_source": MAX_PER_SOURCE,
            "duplicate_clusters_suppressed": 0,
            "source_suppressed": 0,
            "stale_suppressed": 0,
        },
        "ranking_policy": (
            "76% IDF-weighted relevance + 24% explainable quality, then freshness; "
            "hit count is tracked but has zero ranking weight"
        ),
        "tracking_persisted": False,
    }
    if query_tokens:
        idf = _idf_weights(rows)
        candidates = [item for item in (_candidate_score(query_tokens, row, idf) for row in rows)
                      if item is not None]
        candidates.sort(key=lambda item: (-item["score"], str(item["row"].get("id", ""))))
        eligible = []
        for item in candidates:
            if item["score"] < HIT_AT:
                if item["row"].get("stale"):
                    base["guards"]["stale_suppressed"] += 1
                continue
            if any(_similarity(item["row"], chosen["row"]) >= 0.86 for chosen in eligible):
                base["guards"]["duplicate_clusters_suppressed"] += 1
                continue
            family = _source_family(item["row"].get("source"))
            if sum(_source_family(chosen["row"].get("source")) == family
                   for chosen in eligible) >= MAX_PER_SOURCE:
                base["guards"]["source_suppressed"] += 1
                continue
            eligible.append(item)
            if len(eligible) >= limit:
                break

        for rank, item in enumerate(eligible, 1):
            row = item["row"]
            rid = str(row.get("id", ""))
            use = usage.get("cards", {}).get(rid, {})
            base["hits"].append({
                "rank": rank,
                "id": rid,
                "problem": row.get("problem", ""),
                "solution": row.get("solution", ""),
                "tags": list(row.get("tags", [])),
                "source": row.get("source", "?"),
                "score": item["score"],
                "score_components": item["score_components"],
                "reasons": item["reasons"],
                "freshness": {
                    "first_seen": row.get("first_seen", row.get("ts")),
                    "last_verified": row.get("last_verified", row.get("ts")),
                    "stale": bool(row.get("stale")),
                },
                "reuse": {
                    "hit_count": int(use.get("hit_count", 0)),
                    "last_hit_at": use.get("last_hit_at"),
                    "reinforcement_count": int(row.get("reinforcement_count", 1)),
                },
            })

    base["decision"] = "hit" if base["hits"] else "miss"
    if record_hits:
        base["tracking_persisted"] = record_reuse(
            [hit["id"] for hit in base["hits"]],
            {str(row.get("id", "")) for row in rows},
        )
    return base


def _vault_mirror_health(rows):
    result = {
        "available": False,
        "folder": None,
        "expected_active_count": len(rows),
        "active_count": 0,
        "active_bytes": 0,
        "missing_active_count": 0,
        "orphaned_count": 0,
        "orphaned_bytes": 0,
        "total_count": 0,
        "total_bytes": 0,
        "index_bytes": 0,
    }
    try:
        vp = vault_path()
    except (OSError, ValueError):
        return result
    if not vp or not os.path.isdir(vp):
        return result
    folder = os.path.join(vp, "Rune", "Hermes")
    result["available"] = True
    result["folder"] = folder
    expected = {name for name in (_card_filename(row) for row in rows) if name}
    if not os.path.isdir(folder):
        result["missing_active_count"] = len(expected)
        return result
    present_active = set()
    managed_pattern = re.compile(r"^[0-9a-f]{7}(?:[0-9a-f]{5})?-.+\.md$")
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return result
    for entry in entries:
        if entry.name == "_index.md":
            try:
                result["index_bytes"] = entry.stat(follow_symlinks=False).st_size
            except OSError:
                pass
            continue
        if entry.name not in expected and not managed_pattern.match(entry.name):
            continue
        try:
            if not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                continue
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        result["total_count"] += 1
        result["total_bytes"] += size
        if entry.name in expected:
            present_active.add(entry.name)
            result["active_count"] += 1
            result["active_bytes"] += size
        else:
            result["orphaned_count"] += 1
            result["orphaned_bytes"] += size
    result["missing_active_count"] = len(expected - present_active)
    return result


def storage_health():
    """Return bounded-storage and reuse telemetry for UI/API consumers."""
    try:
        rows = load()
        storage_error = None
    except (OSError, ValueError) as exc:
        rows = []
        storage_error = type(exc).__name__
    try:
        quarantined = _load_jsonl(QUARANTINE)
    except (OSError, ValueError):
        quarantined = []
        storage_error = storage_error or "QuarantineUnreadable"
    usage = _usage_state()
    archive = _archive_index()
    limits = _limits()
    active_bytes = os.path.getsize(SOLVED) if os.path.exists(SOLVED) else 0
    quarantine_bytes = os.path.getsize(QUARANTINE) if os.path.exists(QUARANTINE) else 0
    usage_bytes = os.path.getsize(USAGE) if os.path.exists(USAGE) else 0
    archive_bytes = _archive_total_bytes()
    scores = [float(_quality_for_row(row).get("score", 0.0)) for row in rows]
    queries = int(usage.get("queries", 0))
    hit_queries = int(usage.get("hit_queries", 0))
    writes = usage.get("writes", {})
    vault_mirrors = _vault_mirror_health(rows)
    mirror_degraded = (
        vault_mirrors["available"] and
        (vault_mirrors["missing_active_count"] > 0 or vault_mirrors["orphaned_count"] > 0)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "hermes.storage_health",
        "status": "degraded" if storage_error or mirror_degraded else "ok",
        "storage_error": storage_error,
        "cards": {
            "active_count": len(rows),
            "active_bytes": active_bytes,
            "stale_count": sum(bool(row.get("stale")) for row in rows),
        },
        "outcomes": {
            "accepted_count": len(rows),
            "accepted_writes": int(writes.get("accepted", 0)),
            "quarantined_count": len(quarantined),
            "quarantined_writes": int(writes.get("quarantined", 0)),
            "rejected_writes": int(writes.get("rejected", 0)),
            "merged_writes": int(writes.get("merged", 0)),
            "merged_reinforcements": sum(
                max(0, int(row.get("reinforcement_count", 1)) - 1) for row in rows
            ),
            "archived_count": int(archive.get("archived_count", 0)),
        },
        "budget": {
            "max_cards": limits["max_cards"],
            "max_bytes": limits["max_bytes"],
            "count_utilization": round(len(rows) / limits["max_cards"], 4),
            "byte_utilization": round(active_bytes / limits["max_bytes"], 4),
        },
        "quarantine": {
            "path": QUARANTINE,
            "bytes": quarantine_bytes,
            "max_bytes": limits["quarantine_max_bytes"],
            "utilization": round(quarantine_bytes / limits["quarantine_max_bytes"], 4),
        },
        "archive": {
            "directory": ARCHIVE_DIR,
            "bytes": archive_bytes,
            "max_bytes": limits["archive_max_bytes"],
            "utilization": round(archive_bytes / limits["archive_max_bytes"], 4),
            "capacity_exhausted": archive_bytes >= limits["archive_max_bytes"],
            "solved_count": int(archive.get("solved_count", 0)),
            "quarantine_count": int(archive.get("quarantine_count", 0)),
        },
        "vault_mirrors": vault_mirrors,
        "quality": {
            "threshold": QUALITY_AT,
            "average_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "below_threshold_count": sum(score < QUALITY_AT for score in scores),
        },
        "usage": {
            "queries": queries,
            "hit_queries": hit_queries,
            "miss_queries": int(usage.get("miss_queries", 0)),
            "hit_rate": round(hit_queries / queries, 4) if queries else 0.0,
            "tracked_cards": len(usage.get("cards", {})),
            "bytes": usage_bytes,
            "last_query_at": usage.get("last_query_at"),
        },
    }


def cmd_note(problem, solution, tags, source, json_mode=False):
    result = note_memory(problem, solution, tags, source)
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    outcome = result["outcome"]
    rid = result.get("id") or "-"
    if outcome == "accepted":
        print("noted [%s] %s" % (rid, str(problem)[:70]))
        path = card_path(next((row for row in load() if row.get("id") == rid),
                              {"id": rid, "problem": problem}))
        if path and os.path.exists(path):
            print("card: %s" % path)
    elif outcome == "merged":
        print("reinforced [%s] %s" % (rid, str(problem)[:70]))
    else:
        print("%s [%s] %s (quality %.2f)" % (
            outcome, rid, result["reason_code"], result["quality"].get("score", 0.0)
        ))
    # Memory writes are best effort. Policy rejection must not fail a mission.
    return 0


def cmd_query(text, json_mode=False, limit=3):
    result = query_memory(text, limit=limit, record_hits=True)
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result["decision"] == "miss":
        if not tokens(text):
            print("MISS (empty query)")
        else:
            print("MISS: no prior solution for %r" % text)
            print("(solve it, then: python hermes/hermes.py note \"<problem>\" \"<solution>\" ...)")
    else:
        for hit in result["hits"]:
            flag = " [STALE]" if hit["freshness"]["stale"] else ""
            print("HIT %.2f [%s]%s %s" % (
                hit["score"], hit["id"], flag, hit["problem"]
            ))
            print("    -> %s" % hit["solution"])
    return 0 if result["decision"] == "hit" else 1


def cmd_stale(rid):
    with _file_lock(SOLVED):
        rows = load()
        for row in rows:
            if row.get("id") == rid:
                row["stale"] = True
                row["updated_at"] = _now()
                save(rows)
                try:
                    write_card(row)
                    write_index(rows)
                except OSError:
                    pass
                print("marked stale: [%s] %s" % (rid, row.get("problem", "")[:70]))
                return 0
    print("no such id: " + rid)
    return 1


def _pop_option(args, name, default=None):
    if name not in args:
        return default
    index = args.index(name)
    if index + 1 >= len(args):
        raise ValueError("%s requires a value" % name)
    value = args[index + 1]
    del args[index:index + 2]
    return value


def main(argv):
    argv = list(argv)
    cmd = argv[0] if argv else "list"
    args = argv[1:]
    json_mode = "--json" in args
    args = [arg for arg in args if arg != "--json"]
    try:
        if cmd == "query":
            limit = _pop_option(args, "--limit", 3)
            return cmd_query(" ".join(args), json_mode=json_mode, limit=limit)
        if cmd == "note":
            tags = _pop_option(args, "--tags", "")
            source = _pop_option(args, "--source", "")
            if len(args) < 2:
                print(__doc__)
                return 1
            return cmd_note(args[0], args[1], tags, source, json_mode=json_mode)
        if cmd == "stale":
            if not args:
                print(__doc__)
                return 1
            return cmd_stale(args[0])
        if cmd == "stats":
            result = storage_health()
            if json_mode:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print("%d active / %d max · %.1f%% bytes · %d quarantined · %d archived" % (
                    result["cards"]["active_count"], result["budget"]["max_cards"],
                    result["budget"]["byte_utilization"] * 100,
                    result["outcomes"]["quarantined_count"],
                    result["outcomes"]["archived_count"],
                ))
                print("queries: %d · hit rate: %.1f%% · avg quality: %.2f" % (
                    result["usage"]["queries"], result["usage"]["hit_rate"] * 100,
                    result["quality"]["average_score"],
                ))
            return 0
        if cmd == "list":
            for row in load():
                flag = " [STALE]" if row.get("stale") else ""
                print("[%s]%s %s" % (row.get("id", "?"), flag, row.get("problem", "")))
            return 0
    except (OSError, TimeoutError, ValueError) as exc:
        if json_mode:
            print(json.dumps({
                "schema_version": SCHEMA_VERSION, "kind": "hermes.error",
                "error": type(exc).__name__, "detail": str(exc),
            }, ensure_ascii=False))
        else:
            print("Hermes error: %s" % str(exc))
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
