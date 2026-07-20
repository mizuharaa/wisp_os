#!/usr/bin/env python3
"""Focused, offline regression tests for shared task recovery.

No API key, Claude process, browser, or repository mutation is required. Run:

    python test_runtime_recovery.py
"""
import json
import importlib.util
import datetime
import os
import signal
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
os.environ["RUNE_DISABLE_BOOT_RECOVERY"] = "1"
os.environ["RUNE_DISABLE_VERIFIER"] = "1"
os.environ["RUNE_DISABLE_AI_REVIEW"] = "1"
os.environ["RUNE_DISABLE_REPLAN"] = "1"

import runtime as agent_runtime
import ceo
import orchestrator
import serve


class InlineThread:
    """Thread stand-in that makes resume tests deterministic."""
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target, self.args = target, args
        self.started = False

    def start(self):
        self.started = True
        self.target(*self.args)

    def is_alive(self):
        return False


def role(**patch):
    value = {
        "id": "eng", "title": "Engineer", "mission": "Fix the local unit-test bug.",
        "model": "haiku", "turns": 10, "depends_on": [], "review": False,
        "status": "pending", "result": "", "secs": 0, "cost": 0,
    }
    value.update(patch)
    return value


def mission(cid="m1", roles=None, **patch):
    value = {
        "cid": cid, "name": "recovery test", "summary": "", "goal": "fix local test",
        "refined": "fix local test", "keywords": "local test", "recall": False,
        "roles": [role()] if roles is None else roles, "route": "delegate",
        "opts": {}, "account_pref": "auto", "status": "running", "cost": 0,
        "auto_recover": True, "planning_attempt": 0, "planning_history": [],
        "started": "2026-07-15T10:00:00",
    }
    value.update(patch)
    return value


class CeoRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-recovery-test-")
        self.old_cdir, self.old_adir, self.old_approvals = (
            ceo.CDIR, ceo.ADIR, ceo.APPROVALS)
        ceo.CDIR = self.temp.name
        ceo.ADIR = os.path.join(self.temp.name, "archive")
        ceo.APPROVALS = os.path.join(self.temp.name, "approvals.json")
        ceo.LIVE.clear()
        self.patches = [
            mock.patch.object(ceo, "emit", lambda *a, **kw: None),
            mock.patch.object(ceo, "_wait_retry", lambda *a, **kw: True),
            mock.patch.object(
                ceo, "_codex_fallback_status",
                lambda *a, **kw: (False, "Codex unavailable in default test fixture.")),
            mock.patch.object(
                ceo.delivery, "capture_git_baseline",
                lambda _workdir: {"available": False, "reason": "test fixture",
                                  "captured_at": "2026-07-16T00:00:00+00:00"}),
            mock.patch.object(ceo.pulse, "least_used", lambda: ""),
            mock.patch.object(ceo.pulse, "dir_for", lambda _name: ""),
            # Recovery tests must never write candidates into the live brain.
            mock.patch.object(ceo, "note_memory", lambda *_a, **_kw: {
                "outcome": "quarantined", "id": "test-note",
                "reason_code": "quality_below_threshold",
                "quality": {"score": 0.1, "signals": []}}),
            mock.patch.object(ceo.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr="")),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        ceo.LIVE.clear()
        ceo.CDIR, ceo.ADIR, ceo.APPROVALS = (
            self.old_cdir, self.old_adir, self.old_approvals)
        self.temp.cleanup()

    def save(self, value):
        ceo._save(value)
        return value

    def load(self, cid="m1"):
        path = ceo._path(cid)
        if not os.path.isfile(path):
            path = os.path.join(ceo.ADIR, cid + ".json")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def make_live(self, cid="m1"):
        ceo.LIVE[cid] = {"thread": threading.current_thread(), "proc": None,
                         "stop": False, "gate": {}}

    def test_planner_retries_then_starts_roles(self):
        self.save(mission(roles=[], status="planning"))
        plan = {
            "name": "fixed plan", "summary": "one safe role",
            "roles": [{"id": "eng", "title": "Engineer", "mission": "local fix",
                       "model": "haiku", "turns": 10, "depends_on": [],
                       "review": False}],
        }
        calls, ran = [], []

        def fake_api(*_args, **_kwargs):
            calls.append(1)
            return {"error": "API 503 service unavailable"} if len(calls) == 1 else plan

        with mock.patch.object(ceo, "_api", fake_api), \
                mock.patch.object(ceo, "_recall", lambda _text: ""), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            ceo._plan_then_run("m1")

        got = self.load()
        self.assertEqual(len(calls), 2)
        self.assertEqual([x["status"] for x in got["planning_history"]],
                         ["failed", "done"])
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["roles"][0]["status"], "pending")
        self.assertEqual(ran, ["m1"])

    def test_planner_permission_failure_pauses_without_retry(self):
        self.save(mission(roles=[], status="planning"))
        calls = []

        def denied(*_args, **_kwargs):
            calls.append(1)
            return {"error": "credentials required: missing API key"}

        with mock.patch.object(ceo, "_api", denied), \
                mock.patch.object(ceo, "_recall", lambda _text: ""):
            ceo._plan_then_run("m1")

        got = self.load()
        self.assertEqual(len(calls), 1)
        self.assertEqual(got["status"], "waiting_permission")
        self.assertEqual(got["planning_history"][0]["classification"], "permission")
        self.assertIn("prerequisite", got["next_action"].lower())
        request = got["permission_request"]
        self.assertEqual(request["kind"], "planner")
        self.assertFalse(request["can_authorize"])
        self.assertIn("cannot be authorized", ceo.action(
            "m1", "", "allow", request_id=request["request_id"]))
        resumed = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_plan_then_run", lambda cid: resumed.append(cid)):
            self.assertIsNone(ceo.action(
                "m1", "", "retry", request_id=request["request_id"]))
        self.assertEqual(resumed, ["m1"])

    def test_malformed_role_list_is_retried_safely(self):
        self.save(mission(roles=[], status="planning"))
        plan = {
            "name": "fixed plan", "summary": "one safe role",
            "roles": [{"id": "eng", "title": "Engineer", "mission": "local fix",
                       "model": "haiku", "turns": 10, "depends_on": [],
                       "review": False}],
        }
        replies = iter([{"name": "bad", "summary": "bad", "roles": "eng"}, plan])
        ran = []
        with mock.patch.object(ceo, "_api", lambda *_a, **_kw: next(replies)), \
                mock.patch.object(ceo, "_recall", lambda _text: ""), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            ceo._plan_then_run("m1")

        got = self.load()
        self.assertEqual(len(got["planning_history"]), 2)
        self.assertIn("malformed roles", got["planning_history"][0]["detail"])
        self.assertEqual(got["status"], "running")
        self.assertEqual(ran, ["m1"])

    def test_legacy_roleless_planner_request_can_never_allow_bypass(self):
        request_id = "pr_" + "a" * 32
        value = mission(roles=[], status="waiting_permission",
                        permission_request={
                            "request_id": request_id, "kind": "provider",
                            "scope": "provider-tools", "can_authorize": True,
                            "status": "pending"})
        self.save(value)
        public = ceo.public_run(value)["permission_request"]
        self.assertEqual(public["kind"], "planner")
        self.assertFalse(public["can_authorize"])
        error = ceo.action("m1", "", "allow", request_id=request_id)
        self.assertIn("cannot be authorized", error)
        self.assertEqual(self.load()["status"], "waiting_permission")

    def test_empty_role_planning_failure_can_resume(self):
        self.save(mission(roles=[], status="error", detail="planner unavailable"))
        calls = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_plan_then_run", lambda cid: calls.append(cid)):
            self.assertIsNone(ceo.resume("m1"))
        got = self.load()
        self.assertEqual(calls, ["m1"])
        self.assertEqual(got["status"], "planning")
        self.assertEqual(got["resumes"], 1)

    def test_task_failure_runs_fixer_then_original_verifies(self):
        self.save(mission())
        self.make_live()
        replies = iter([
            {"is_error": True, "result": "unit test assertion failed"},
            {"is_error": False, "result": "RECOVERY REPORT: fixed local fixture; focused test passed"},
            {"is_error": False, "result": "original role complete; focused test passed"},
        ])
        with mock.patch.object(ceo, "_worker", lambda *a, **kw: next(replies)):
            ceo._run("m1")
        got = self.load()
        item = got["roles"][0]
        self.assertEqual(got["status"], "done")
        self.assertEqual(item["status"], "done")
        self.assertEqual(len(item["attempts"]), 2)
        self.assertEqual(len(item["recovery_history"]), 1)
        self.assertEqual(item["recovery_history"][0]["verification"],
                         "passed-original-rerun")
        self.assertEqual(item["recovery_history"][0]["failure_class"], "task")
        self.assertEqual(item["recovery_history"][0]["repair_class"], "success")
        self.assertTrue(item["recovery_history"][0]["learnable"])
        self.assertIn("verification=passed-original-rerun", item["recovery_summary"])
        self.assertFalse(os.path.exists(ceo._path("m1")))
        self.assertTrue(os.path.isfile(os.path.join(ceo.ADIR, "m1.json")))
        self.assertTrue(got.get("finished_at"))
        history = ceo.list_history()
        self.assertEqual([entry["cid"] for entry in history], ["m1"])
        self.assertTrue(history[0]["archived"])

    def test_failed_role_triggers_one_replan_from_the_ledger(self):
        self.save(mission())
        self.make_live()
        workers = iter([
            {"is_error": True, "result": "unit test assertion failed one"},
            {"is_error": False, "result": "RECOVERY REPORT: adjusted fixture one"},
            {"is_error": True, "result": "unit test assertion failed two"},
            {"is_error": False, "result": "RECOVERY REPORT: adjusted fixture two"},
            {"is_error": True, "result": "unit test assertion failed three"},
            {"is_error": False,
             "result": "replacement role finished; focused test passed"},
        ])
        plans = []

        def fake_api(model, system, user, schema, **_kw):
            plans.append((system, user))
            return {"name": "replan", "summary": "",
                    "roles": [{"id": "fresh-fixer", "title": "Fresh fixer",
                                "mission": "Take the different approach.",
                                "model": "haiku", "turns": 10,
                                "depends_on": [], "review": False}]}

        with mock.patch.dict(os.environ, {"RUNE_DISABLE_REPLAN": "0"}), \
                mock.patch.object(ceo, "_worker", lambda *a, **kw: next(workers)), \
                mock.patch.object(ceo, "_api", fake_api):
            ceo._run("m1")

        got = self.load()
        by_id = {r["id"]: r for r in got["roles"]}
        self.assertEqual(got["replans"], 1)
        self.assertEqual(got["status"], "done")
        self.assertEqual(by_id["eng"]["status"], "skipped")
        self.assertIn("superseded", by_id["eng"]["detail"])
        self.assertEqual(by_id["r2-fresh-fixer"]["status"], "done")
        self.assertEqual(len(plans), 1)
        self.assertIn("ROLE LEDGER", plans[0][1])
        self.assertIn("unit test assertion failed three", plans[0][1])
        self.assertIn("MID-MISSION REPLAN", plans[0][0])

    def test_verifier_sends_incomplete_work_back_once(self):
        self.save(mission())
        self.make_live()
        workers = iter([
            {"is_error": False, "result": "I plan to update the files next."},
            {"is_error": False,
             "result": "Updated app.py and ran the focused test; it passed."},
        ])
        verdicts = iter([
            {"verdict": "revise",
             "feedback": "Nothing was changed — edit app.py and run the test."},
            {"verdict": "accept", "feedback": ""},
        ])
        with mock.patch.dict(os.environ, {"RUNE_DISABLE_VERIFIER": "0"}), \
                mock.patch.object(ceo, "_worker", lambda *a, **kw: next(workers)), \
                mock.patch.object(ceo, "_api", lambda *a, **kw: next(verdicts)):
            ceo._run("m1")
        got = self.load()
        item = got["roles"][0]
        self.assertEqual(got["status"], "done")
        self.assertEqual(item["status"], "done")
        self.assertEqual(item["verifies"], 1)
        self.assertEqual(item["verification"]["verdict"], "accept")
        self.assertIn("VERIFIER FEEDBACK", item["mission"])
        self.assertEqual(len(item["attempts"]), 2)

    def test_unavailable_verifier_never_blocks_completion(self):
        self.save(mission())
        self.make_live()
        with mock.patch.dict(os.environ, {"RUNE_DISABLE_VERIFIER": "0"}), \
                mock.patch.object(ceo, "_worker", return_value={
                    "is_error": False, "result": "focused test passed"}), \
                mock.patch.object(ceo, "_api",
                                  return_value={"error": "API 500: down"}):
            ceo._run("m1")
        got = self.load()
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["roles"][0]["verification"]["verdict"], "accept")

    def test_delivery_setup_failure_never_turns_success_into_error(self):
        self.save(mission())
        self.make_live()
        with mock.patch.object(
                ceo, "_worker",
                return_value={"is_error": False, "result": "focused test passed"}), \
                mock.patch.object(
                    ceo.delivery, "initialize_completed_delivery",
                    side_effect=RuntimeError("metadata setup broke")):
            ceo._run("m1")

        got = self.load()
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["delivery"]["status"], "unavailable")
        self.assertTrue(os.path.isfile(os.path.join(ceo.ADIR, "m1.json")))

    def test_task_fixer_is_capped_at_two_cycles(self):
        self.save(mission())
        self.make_live()
        replies = iter([
            {"is_error": True, "result": "unit test assertion failed one"},
            {"is_error": False, "result": "RECOVERY REPORT: adjusted fixture one"},
            {"is_error": True, "result": "unit test assertion failed two"},
            {"is_error": False, "result": "RECOVERY REPORT: adjusted fixture two"},
            {"is_error": True, "result": "unit test assertion failed three"},
        ])
        with mock.patch.object(ceo, "_worker", lambda *a, **kw: next(replies)):
            ceo._run("m1")
        got = self.load()
        item = got["roles"][0]
        self.assertEqual(got["status"], "failed")
        self.assertEqual(item["status"], "failed")
        self.assertEqual(len(item["recovery_history"]), 2)
        self.assertEqual(len(item["attempts"]), 3)
        self.assertIn("recovery budget exhausted", item["detail"])

    def test_transient_retry_budget_is_capped(self):
        self.save(mission())
        self.make_live()
        calls = []

        def failed(*_args, **_kwargs):
            calls.append(1)
            return {"is_error": True, "result": "connection reset by peer"}

        with mock.patch.object(ceo, "_worker", failed):
            ceo._run("m1")
        got = self.load()
        item = got["roles"][0]
        self.assertEqual(len(calls), 1 + ceo.MAX_TRANSIENT_RETRIES)
        self.assertEqual(len(item["attempts"]), 1 + ceo.MAX_TRANSIENT_RETRIES)
        self.assertFalse(item.get("recovery_history"))
        self.assertEqual(item["status"], "failed")
        self.assertIn("transient retry budget exhausted", item["detail"])

    def test_claude_weekly_limit_continues_once_through_codex(self):
        self.save(mission(safe_permissions=True))
        self.make_live()
        calls = []

        def worker(_cid, current, context, _cfg, **kwargs):
            calls.append({
                "model": current.get("model"),
                "provider": current.get("provider") or
                            ceo._provider_for_model(current.get("model")),
                "context": context,
                "safe_permissions": kwargs.get("safe_permissions"),
            })
            if len(calls) == 1:
                return {"is_error": True, "provider": "claude",
                        "session_id": "claude-session",
                        "result": "You've hit your weekly usage limit; reset later."}
            return {"is_error": False, "provider": "codex",
                    "session_id": "codex-thread",
                    "result": "Implemented the fix and the focused test passed."}

        with mock.patch.object(ceo, "_worker", worker), \
                mock.patch.object(
                    ceo, "_codex_fallback_status",
                    return_value=(True, "Codex is ready.")):
            ceo._run("m1")

        got = self.load()
        item = got["roles"][0]
        self.assertEqual(got["status"], "done")
        self.assertEqual(len(calls), 2)
        self.assertEqual([(call["provider"], call["model"]) for call in calls],
                         [("claude", "haiku"),
                          ("codex", ceo.CODEX_FALLBACK_MODEL)])
        self.assertIn("Provider failover", calls[1]["context"])
        self.assertTrue(calls[1]["safe_permissions"])
        self.assertEqual(item["provider"], "codex")
        self.assertEqual(item["model"], ceo.CODEX_FALLBACK_MODEL)
        self.assertEqual(item["session"], "codex-thread")
        self.assertEqual(item["provider_fallback"]["count"], 1)
        self.assertEqual(item["provider_fallback"]["label"], "Claude → Codex")
        self.assertEqual(item["provider_fallback"]["from_model"], "haiku")
        self.assertEqual(item["provider_fallback"]["from_session"],
                         "claude-session")
        self.assertEqual(item["provider_fallback"]["status"], "succeeded")
        self.assertIn("Codex completed", item["provider_fallback"]["summary"])
        self.assertEqual([attempt["kind"] for attempt in item["attempts"]],
                         ["worker", "provider_fallback"])
        self.assertEqual([attempt["provider"] for attempt in item["attempts"]],
                         ["claude", "codex"])
        self.assertEqual(got["providers"], ["codex"])

    def test_failed_codex_fallback_retries_codex_without_switching_back(self):
        self.save(mission())
        self.make_live()
        providers = []

        def worker(_cid, current, *_args, **_kwargs):
            provider = current.get("provider") or ceo._provider_for_model(
                current.get("model"))
            providers.append(provider)
            if len(providers) == 1:
                return {"is_error": True, "provider": "claude",
                        "result": "weekly limit reached"}
            if len(providers) == 2:
                return {"is_error": True, "provider": "codex",
                        "result": "connection reset by peer"}
            return {"is_error": False, "provider": "codex",
                    "result": "Codex resumed and completed the role."}

        with mock.patch.object(ceo, "_worker", worker), \
                mock.patch.object(
                    ceo, "_codex_fallback_status",
                    return_value=(True, "Codex is ready.")):
            ceo._run("m1")

        got = self.load()
        item = got["roles"][0]
        self.assertEqual(providers, ["claude", "codex", "codex"])
        self.assertEqual(item["provider_fallback"]["count"], 1)
        self.assertEqual(item["provider_fallback"]["status"], "succeeded")
        self.assertEqual(item["status"], "done")

    def test_weekly_limit_stays_bounded_when_codex_is_unavailable(self):
        self.save(mission())
        self.make_live()
        providers = []

        def limited(_cid, current, *_args, **_kwargs):
            providers.append(current.get("provider") or
                             ceo._provider_for_model(current.get("model")))
            return {"is_error": True, "provider": "claude",
                    "result": "weekly limit reached"}

        with mock.patch.object(ceo, "_worker", limited), \
                mock.patch.object(
                    ceo, "_codex_fallback_status",
                    return_value=(False, "Codex weekly capacity is exhausted.")):
            ceo._run("m1")

        got = self.load()
        item = got["roles"][0]
        self.assertEqual(providers, ["claude"] * (1 + ceo.MAX_TRANSIENT_RETRIES))
        self.assertNotIn("provider_fallback", item)
        self.assertEqual(item["fallback_unavailable"]["provider"], "codex")
        self.assertIn("exhausted", item["fallback_unavailable"]["reason"])
        self.assertEqual(item["status"], "failed")

    def test_permission_failure_never_launches_fixer(self):
        self.save(mission())
        self.make_live()
        calls = []

        def denied(*_args, **_kwargs):
            calls.append(1)
            return {"is_error": True,
                    "result": "MAESTRO GUARD: blocked gated action 'deploy' — requires approval"}

        with mock.patch.object(ceo, "_worker", denied):
            ceo._run("m1")
        got = self.load()
        self.assertEqual(len(calls), 1)
        self.assertEqual(got["status"], "waiting_permission")
        self.assertEqual(got["roles"][0]["status"], "waiting_permission")
        self.assertFalse(got["roles"][0].get("recovery_history"))

    def test_success_envelope_with_terminal_permission_request_is_gated(self):
        providers = (
            ("claude", "haiku"),
            ("codex", ceo.CODEX_FALLBACK_MODEL),
        )
        for index, (provider, model) in enumerate(providers):
            cid = "ask%d" % index
            item = role(model=model, provider=provider)
            self.save(mission(cid=cid, roles=[item], permission_mode="skip",
                              safe_permissions=False))
            self.make_live(cid)
            result = {
                "is_error": False, "provider": provider,
                "result": "I need your permission to continue with this command.",
            }
            with self.subTest(provider=provider), \
                    mock.patch.object(ceo, "_worker", return_value=result):
                ceo._run(cid)
            got = self.load(cid)
            current = got["roles"][0]
            self.assertEqual(got["status"], "waiting_permission")
            self.assertEqual(current["status"], "waiting_permission")
            self.assertEqual(current["attempts"][-1]["classification"], "permission")
            self.assertEqual(current["attempts"][-1]["status"], "gated")
            self.assertFalse(current.get("recovery_history"))
            self.assertTrue(os.path.isfile(ceo._path(cid)))

    def test_permission_gate_parks_before_independent_role_starts(self):
        roles = [role(id="first", title="First"),
                 role(id="second", title="Second")]
        self.save(mission(cid="park", roles=roles, permission_mode="skip",
                          safe_permissions=False))
        self.make_live("park")
        calls = []

        def worker(_cid, current, *_args, **_kwargs):
            calls.append(current["id"])
            return ({"is_error": False, "provider": "claude",
                     "result": "I need permission to continue"}
                    if current["id"] == "first" else
                    {"is_error": False, "provider": "claude", "result": "done"})

        with mock.patch.object(ceo, "_worker", worker), \
                mock.patch.object(
                    ceo, "note_memory",
                    side_effect=AssertionError("permission waits must not learn")):
            ceo._run("park")
        got = self.load("park")
        self.assertEqual(calls, ["first"])
        self.assertEqual(got["roles"][0]["status"], "waiting_permission")
        self.assertEqual(got["roles"][1]["status"], "pending")
        self.assertNotIn("park", ceo.LIVE)
        self.assertNotIn("learning_receipt", got)

        # Once _run settles, the persisted decision is immediately accepted.
        request_id = got["roles"][0]["permission_request"]["request_id"]
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda _cid: None):
            self.assertIsNone(ceo.action(
                "park", "first", "deny", request_id=request_id))

    def test_successful_historical_permission_mention_remains_success(self):
        self.save(mission())
        self.make_live()
        with mock.patch.object(ceo, "_worker", return_value={
                "is_error": False,
                "result": "Fixed the permission denied handling and tests pass.",
                "provider": "claude"}):
            ceo._run("m1")
        got = self.load()
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["roles"][0]["attempts"][-1]["classification"],
                         "success")

    def test_persisted_guard_permission_can_be_scoped_and_resumed(self):
        waiting = role(status="waiting_permission", result="", detail="blocked")
        waiting["permission_request"] = ceo._permission_request(
            "MAESTRO GUARD: blocked gated action 'deploy'.", "eng")
        dependent = role(id="verify", title="Verifier", status="blocked",
                         depends_on=["eng"], detail="blocked: eng didn't finish")
        value = mission(status="waiting_permission", roles=[waiting, dependent],
                        safe_permissions=True, permission_mode="safe")
        value["permission_request"] = dict(waiting["permission_request"])
        request_id = waiting["permission_request"]["request_id"]
        self.save(value)
        ran = []

        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            self.assertIsNone(ceo.action(
                "m1", "eng", "allow", request_id=request_id))

        got = self.load()
        self.assertEqual(ran, ["m1"])
        self.assertEqual(got["permission_mode"], "safe")
        self.assertTrue(got["safe_permissions"])
        self.assertNotIn("permission_mode", got["roles"][0])
        self.assertEqual(ceo._permission_mode_for(got, got["roles"][0]), "safe")
        self.assertEqual(got["roles"][0]["status"], "pending")
        self.assertEqual(got["roles"][1]["status"], "pending")
        self.assertEqual(got["permission_request"]["status"], "allowed")
        self.assertEqual(got["permission_authorizations"][-1]["scope"], "deploy")
        with open(ceo.APPROVALS, encoding="utf-8") as handle:
            token = json.load(handle)["tokens"][-1]
        self.assertEqual((token["action"], token["cid"], token["role"]),
                         ("deploy", "m1", "eng"))
        self.assertEqual(token["request_id"], request_id)
        self.assertNotIn("MAESTRO GUARD", json.dumps(
            got["permission_authorizations"]))
        self.assertIn("still settling", ceo.action(
            "m1", "eng", "allow", request_id=request_id))
        switched = dict(got["roles"][0])
        ceo._switch_role_to_codex(switched, "weekly limit")
        self.assertEqual(ceo._permission_mode_for(got, switched), "safe")
        argv = ceo._worker_argv(
            switched, "safe", self.temp.name, output_path=os.path.join(
                self.temp.name, "codex-result.txt"))
        self.assertNotIn("--yolo", argv)

    def test_legacy_permission_wait_derives_server_scope_on_allow(self):
        waiting = role(
            status="waiting_permission",
            result="MAESTRO GUARD: blocked gated action 'external-send'.",
            detail="operator permission or credentials are required")
        self.save(mission(status="waiting_permission", roles=[waiting],
                          safe_permissions=False, permission_mode="skip"))
        request_id = ceo.public_run(self.load())["permission_request"]["request_id"]
        ran = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            self.assertIsNone(ceo.action(
                "m1", "eng", "allow", request_id=request_id))
        got = self.load()
        self.assertEqual(ran, ["m1"])
        self.assertEqual(got["permission_request"]["kind"], "guard")
        self.assertEqual(got["permission_request"]["scope"], "external-send")
        with open(ceo.APPROVALS, encoding="utf-8") as handle:
            token = json.load(handle)["tokens"][-1]
        self.assertEqual(token["action"], "external-send")

    def test_nonce_bearing_legacy_requests_are_narrowed_from_evidence(self):
        credential_id = "pr_" + "b" * 32
        old_request = {
            "request_id": credential_id, "kind": "provider",
            "scope": "provider-tools", "can_authorize": True,
            "status": "pending",
        }
        waiting = role(
            status="waiting_permission",
            result="Authentication is required; please enter your password",
            permission_request=dict(old_request))
        value = mission(status="waiting_permission", roles=[waiting],
                        permission_request=dict(old_request))
        self.save(value)
        public = ceo.public_run(value)["permission_request"]
        self.assertEqual(public["request_id"], credential_id)
        self.assertEqual(public["kind"], "credential")
        self.assertFalse(public["can_authorize"])
        self.assertIn("cannot be authorized", ceo.action(
            "m1", "eng", "allow", request_id=credential_id))
        self.assertNotIn("permission_authorizations", self.load())

        guard_id = "pr_" + "c" * 32
        guard_request = dict(old_request, request_id=guard_id)
        guard_role = role(
            status="waiting_permission",
            result="MAESTRO GUARD: blocked gated action 'deploy'.",
            permission_request=dict(guard_request))
        guard_run = mission(cid="guardold", status="waiting_permission",
                            roles=[guard_role],
                            permission_request=dict(guard_request))
        self.save(guard_run)
        public = ceo.public_run(guard_run)["permission_request"]
        self.assertEqual((public["kind"], public["scope"]), ("guard", "deploy"))
        ran = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            self.assertIsNone(ceo.action(
                "guardold", "eng", "allow", request_id=guard_id))
        self.assertEqual(ran, ["guardold"])
        with open(ceo.APPROVALS, encoding="utf-8") as handle:
            token = json.load(handle)["tokens"][-1]
        self.assertEqual((token["action"], token["request_id"]),
                         ("deploy", guard_id))

    def test_stale_permission_click_cannot_authorize_a_new_gate(self):
        waiting = role(status="waiting_permission")
        waiting["permission_request"] = ceo._permission_request(
            "permission prompt requires approval for command one", "eng")
        value = mission(status="waiting_permission", roles=[waiting],
                        permission_mode="safe", safe_permissions=True)
        value["permission_request"] = dict(waiting["permission_request"])
        old_request_id = waiting["permission_request"]["request_id"]
        self.save(value)
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda _cid: None):
            self.assertIsNone(ceo.action(
                "m1", "eng", "allow", request_id=old_request_id))

        # Simulate the launched retry immediately reaching a distinct gate.
        ceo.LIVE.clear()
        regated = self.load()
        target = regated["roles"][0]
        ceo._set_permission_wait(
            regated, "permission prompt requires approval for command two", target)
        ceo._save(regated)
        new_request_id = regated["permission_request"]["request_id"]
        self.assertNotEqual(old_request_id, new_request_id)

        error = ceo.action(
            "m1", "eng", "allow", request_id=old_request_id)
        self.assertIn("stale", error)
        still_waiting = self.load()
        self.assertEqual(still_waiting["status"], "waiting_permission")
        self.assertEqual(still_waiting["permission_request"]["request_id"],
                         new_request_id)

    def test_permission_action_endpoint_requires_exact_request_id(self):
        waiting = role(status="waiting_permission")
        waiting["permission_request"] = ceo._permission_request(
            "permission prompt requires approval", "eng")
        value = mission(status="waiting_permission", roles=[waiting])
        value["permission_request"] = dict(waiting["permission_request"])
        self.save(value)
        handler = object.__new__(serve.Handler)
        handler._json = lambda status, payload: (status, payload)

        missing = handler.api_ceo_action({
            "cid": "m1", "role": "eng", "action": "allow"})
        self.assertEqual(missing[0], 400)
        forged = handler.api_ceo_action({
            "cid": "m1", "role": "eng", "action": "allow",
            "request_id": "pr_" + "0" * 32})
        self.assertEqual(forged[0], 409)
        self.assertIn("stale", forged[1]["error"])

    def test_credential_permission_cannot_be_approved_but_can_retry(self):
        waiting = role(status="waiting_permission", detail="credential needed")
        waiting["permission_request"] = ceo._permission_request(
            "authentication required: missing API key secret-value", "eng")
        value = mission(status="waiting_permission", roles=[waiting],
                        permission_mode="skip", safe_permissions=False)
        value["permission_request"] = dict(waiting["permission_request"])
        request_id = waiting["permission_request"]["request_id"]
        self.save(value)

        error = ceo.action("m1", "eng", "allow", request_id=request_id)
        self.assertIn("cannot be authorized", error)
        self.assertEqual(self.load()["roles"][0]["status"], "waiting_permission")
        ran = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            self.assertIsNone(ceo.action(
                "m1", "eng", "retry", request_id=request_id))
        got = self.load()
        self.assertEqual(ran, ["m1"])
        self.assertEqual(got["roles"][0]["status"], "pending")
        self.assertNotIn("operator_authorization", got["roles"][0])
        self.assertEqual(got["permission_authorizations"][-1]["decision"], "retry")

    def test_provider_grant_does_not_leak_into_recovery_worker(self):
        future = (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat(
            timespec="seconds")
        authorization = {
            "authorization_id": "auth-provider", "request_id": "pr-provider",
            "decision": "allow", "role_id": "eng", "kind": "provider",
            "scope": "provider-tools", "status": "active", "expires_at": future,
        }
        item = role(permission_mode="skip",
                    operator_authorization=dict(authorization))
        value = mission(roles=[item], permission_mode="safe", safe_permissions=True,
                        permission_authorizations=[dict(authorization)])
        self.save(value)
        self.make_live()
        policies = []
        replies = iter((
            {"is_error": True, "provider": "claude",
             "result": "unit test assertion failed"},
            {"is_error": False, "provider": "claude",
             "result": "RECOVERY REPORT: corrected the local fixture and test order"},
            {"is_error": False, "provider": "claude",
             "result": "original role complete; focused tests pass"},
        ))

        def worker(*_args, **kwargs):
            policies.append(kwargs.get("safe_permissions"))
            return next(replies)

        with mock.patch.object(ceo, "_worker", worker):
            ceo._run("m1")
        got = self.load()
        self.assertEqual(policies, [False, True, True])
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["permission_authorizations"][0]["status"], "consumed")

    def test_deny_skips_only_named_permission_role_and_continues(self):
        waiting = role(status="waiting_permission", detail="approval needed")
        waiting["permission_request"] = ceo._permission_request(
            "permission prompt requires approval", "eng")
        other = role(id="other", title="Other", status="done")
        value = mission(status="waiting_permission", roles=[waiting, other])
        value["permission_request"] = dict(waiting["permission_request"])
        request_id = waiting["permission_request"]["request_id"]
        self.save(value)
        ran = []
        with mock.patch.object(ceo.threading, "Thread", InlineThread), \
                mock.patch.object(ceo, "_run", lambda cid: ran.append(cid)):
            self.assertIsNone(ceo.action(
                "m1", "eng", "deny", request_id=request_id))
        got = self.load()
        self.assertEqual(ran, ["m1"])
        self.assertEqual(got["roles"][0]["status"], "skipped")
        self.assertEqual(got["roles"][1]["status"], "done")
        self.assertEqual(got["permission_request"]["status"], "denied")

    def test_recoverable_states_never_auto_archive(self):
        states = ("failed", "stopped", "exhausted", "waiting_permission")
        for index, status in enumerate(states):
            cid = "keep%d" % index
            self.save(mission(cid=cid, status=status,
                              roles=[role(status=status)]))

        with mock.patch.object(ceo, "_age_days", lambda _run: 999):
            listed = ceo.list_all()

        self.assertEqual({run["cid"] for run in listed},
                         {"keep0", "keep1", "keep2", "keep3"})
        self.assertFalse(os.path.isdir(ceo.ADIR))

    def test_manual_archive_is_idempotent_and_history_is_bounded(self):
        first = self.save(mission(cid="old", status="failed"))
        first["finished_at"] = "2026-07-15T10:00:00"
        ceo._save(first)
        recent = self.save(mission(cid="recent", status="done",
                                   roles=[role(status="done")]))
        recent["finished_at"] = "2026-07-16T10:00:00"
        ceo._save(recent)

        self.assertIsNone(ceo.archive("old"))
        self.assertIsNone(ceo.archive("old"))
        history = ceo.list_history(limit=1)
        self.assertEqual([item["cid"] for item in history], ["recent"])
        self.assertFalse(history[0]["archived"])
        self.assertEqual(len(ceo.list_history(limit=ceo.HISTORY_MAX + 500)), 2)

    def test_stop_wins_when_worker_returns_after_cancellation(self):
        self.save(mission())
        self.make_live()

        def late_success(*_args, **_kwargs):
            self.assertIsNone(ceo.action("m1", "eng", "stop"))
            return {"is_error": False, "result": "finished just after cancellation"}

        with mock.patch.object(ceo, "_worker", late_success):
            ceo._run("m1")
        got = self.load()
        self.assertEqual(got["status"], "stopped")
        self.assertEqual(got["roles"][0]["status"], "stopped")

    def test_only_verified_nontrivial_recovery_is_learnable(self):
        candidate = mission(status="done")
        candidate["roles"][0].update(status="done", recovery_history=[{
            "cycle": 1, "repair_class": "success",
            "repair_summary": "fixed it", "verification": "passed-original-rerun",
            "learnable": False,
        }])
        self.assertFalse(ceo._worth_remembering(candidate))
        candidate["roles"][0]["recovery_history"][0].update(
            repair_summary="Corrected stale fixture ordering and verified the focused restart test.",
            learnable=True)
        self.assertTrue(ceo._worth_remembering(candidate))


class OrchestratorRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-orch-test-")
        self.old_odir = orchestrator.ODIR
        orchestrator.ODIR = self.temp.name
        orchestrator.LIVE.clear()
        self.patches = [
            mock.patch.object(orchestrator, "emit", lambda *a, **kw: None),
            mock.patch.object(orchestrator.pulse, "least_used", lambda: ""),
            mock.patch.object(orchestrator.pulse, "dir_for", lambda _name: ""),
            mock.patch.object(orchestrator.agent_runtime, "wait_backoff",
                              lambda *a, **kw: True),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        orchestrator.LIVE.clear()
        orchestrator.ODIR = self.old_odir
        self.temp.cleanup()

    @staticmethod
    def loop(oid="o1"):
        return {
            "oid": oid, "name": "loop", "mission": "fix the local test",
            "dir": "", "model": "default", "critic": "opus", "account": "",
            "turns": 10, "rounds": 1, "auto": True, "skip": False,
            "status": "running", "round": 0, "cost": 0, "turns_log": [],
            "detail": "", "next_action": "", "session_id": None,
            "started": "2026-07-15T10:00:00",
        }

    def test_worker_transient_retries_are_bounded(self):
        value = self.loop()
        orchestrator._save(value)
        orchestrator.LIVE["o1"] = {
            "thread": threading.current_thread(), "proc": None,
            "stop": False, "human": None}
        calls = []

        def failed():
            calls.append(1)
            return {"is_error": True, "result": "503 service unavailable"}

        got = orchestrator._call_with_transient_retries("o1", value, "worker", failed)
        self.assertEqual(len(calls), 1 + orchestrator.MAX_TRANSIENT_RETRIES)
        self.assertEqual(got["classification"], "transient")
        self.assertEqual(got["retry_count"], orchestrator.MAX_TRANSIENT_RETRIES)

    def test_late_worker_completion_cannot_overwrite_stop(self):
        value = self.loop()
        orchestrator._save(value)
        orchestrator.LIVE["o1"] = {
            "thread": threading.current_thread(), "proc": None,
            "stop": False, "human": None}

        def late_success(*_args, **_kwargs):
            self.assertIsNone(orchestrator.action("o1", "stop"))
            return {"is_error": False, "result": "late success"}

        with mock.patch.object(orchestrator, "_claude", late_success):
            orchestrator._run("o1")
        with open(orchestrator._path("o1"), encoding="utf-8") as handle:
            got = json.load(handle)
        self.assertEqual(got["status"], "stopped")


class RuntimeHelperTests(unittest.TestCase):
    def test_public_run_normalizes_legacy_permission_requests_truthfully(self):
        credential_run = mission(
            status="waiting_permission",
            roles=[role(status="waiting_permission",
                        result="authentication required: missing API key")])
        credential = ceo.public_run(credential_run)
        self.assertEqual(credential["roles"][0]["permission_request"]["kind"],
                         "credential")
        self.assertFalse(
            credential["roles"][0]["permission_request"]["can_authorize"])
        self.assertFalse(credential["permission_request"]["can_authorize"])
        self.assertNotIn("permission_request", credential_run["roles"][0],
                         "public normalization must not rewrite persisted state")
        self.assertEqual(
            credential["permission_request"]["request_id"],
            ceo.public_run(credential_run)["permission_request"]["request_id"])

        guard_run = mission(
            status="waiting_permission",
            roles=[role(status="waiting_permission",
                        result="MAESTRO GUARD: blocked gated action 'soul-write'.")])
        guard = ceo.public_run(guard_run)
        self.assertEqual(guard["permission_request"]["kind"], "guard")
        self.assertEqual(guard["permission_request"]["scope"], "soul-write")
        self.assertTrue(guard["permission_request"]["can_authorize"])

        unknown_run = mission(
            status="waiting_permission",
            roles=[role(status="waiting_permission",
                        result="", detail="operator action required")])
        unknown = ceo.public_run(unknown_run)["permission_request"]
        self.assertEqual(unknown["kind"], "unknown")
        self.assertFalse(unknown["can_authorize"])
        self.assertTrue(unknown["request_id"].startswith("legacy_"))

        vague_credential_run = mission(
            status="waiting_permission",
            roles=[role(status="waiting_permission", result="credential needed")])
        vague = ceo.public_run(vague_credential_run)["permission_request"]
        self.assertEqual(vague["kind"], "credential")
        self.assertFalse(vague["can_authorize"])

    def test_scoped_role_skip_expires_and_is_consumed(self):
        run = {"permission_mode": "safe", "safe_permissions": True}
        future = (datetime.datetime.now() + datetime.timedelta(minutes=5)).isoformat(
            timespec="seconds")
        past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat(
            timespec="seconds")
        receipt = {"authorization_id": "auth-a", "request_id": "pr-a",
                   "kind": "provider",
                   "status": "active", "expires_at": future}
        run["permission_authorizations"] = [dict(receipt)]
        item = {"permission_mode": "skip", "operator_authorization": dict(receipt)}
        self.assertEqual(ceo._permission_mode_for(run, item), "skip")
        ceo._consume_operator_authorization(run, item)
        self.assertEqual(item["operator_authorization"]["status"], "consumed")
        self.assertEqual(run["permission_authorizations"][0]["status"], "consumed")
        self.assertTrue(run["permission_authorizations"][0].get("consumed_at"))
        self.assertNotIn("permission_mode", item)
        self.assertEqual(ceo._permission_mode_for(run, item), "safe")
        item.update(permission_mode="skip")
        item["operator_authorization"].update(status="active", expires_at=past)
        self.assertEqual(ceo._permission_mode_for(run, item), "safe")
        item["operator_authorization"].update(status="consumed", expires_at=future)
        self.assertEqual(ceo._permission_mode_for(run, item), "safe")
        item["operator_authorization"].update(
            status="active", kind="guard", expires_at=future)
        self.assertEqual(ceo._permission_mode_for(run, item), "safe")
        self.assertEqual(ceo._permission_mode_for(
            {"permission_mode": "skip"}, item), "skip")

    def test_ceo_guard_token_is_reusable_only_by_exact_mission_role(self):
        spec = importlib.util.spec_from_file_location(
            "rune_test_guard", os.path.join(ROOT, ".claude", "hooks", "guard.py"))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        with tempfile.TemporaryDirectory(prefix="rune-guard-scope-") as temp:
            guard.APPROVALS = os.path.join(temp, "approvals.json")
            expires = 9_999_999_999
            with open(guard.APPROVALS, "w", encoding="utf-8") as handle:
                json.dump({"tokens": [{"action": "deploy", "expires": expires,
                                        "source": "ceo-permission",
                                        "cid": "mission-a", "role": "eng",
                                        "request_id": "pr-a"}]}, handle)
            with mock.patch.dict(os.environ, {
                    "MAESTRO_SID": "mission-a", "MAESTRO_ROLE_ID": "eng",
                    "MAESTRO_PERMISSION_REQUEST_ID": "pr-a"}):
                self.assertTrue(guard.approved("deploy"))
            with mock.patch.dict(os.environ, {
                    "MAESTRO_SID": "mission-a", "MAESTRO_ROLE_ID": "eng",
                    "MAESTRO_PERMISSION_REQUEST_ID": "pr-b"}):
                self.assertFalse(guard.approved("deploy"))
            with mock.patch.dict(os.environ, {
                    "MAESTRO_SID": "mission-b", "MAESTRO_ROLE_ID": "eng"}):
                self.assertFalse(guard.approved("deploy"))
            with mock.patch.dict(os.environ, {
                    "MAESTRO_SID": "mission-a", "MAESTRO_ROLE_ID": "other"}):
                self.assertFalse(guard.approved("deploy"))

            # Manual approve.py tokens remain intentionally operator-global.
            with open(guard.APPROVALS, "w", encoding="utf-8") as handle:
                json.dump({"tokens": [{"action": "deploy", "expires": expires}]}, handle)
            with mock.patch.dict(os.environ, {
                    "MAESTRO_SID": "mission-b", "MAESTRO_ROLE_ID": "other"}):
                self.assertTrue(guard.approved("deploy"))

    def test_worker_session_cannot_invoke_manual_approver(self):
        spec = importlib.util.spec_from_file_location(
            "rune_test_approve", os.path.join(ROOT, ".claude", "hooks", "approve.py"))
        approve = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(approve)
        with tempfile.TemporaryDirectory(prefix="rune-approve-worker-") as temp:
            approve.APPROVALS = os.path.join(temp, "approvals.json")
            with mock.patch.object(sys, "argv", ["approve.py", "deploy"]), \
                    mock.patch.dict(os.environ, {
                        "MAESTRO_SID": "mission-a", "MAESTRO_ROLE_ID": "eng"}):
                self.assertEqual(approve.main(), 2)
            self.assertFalse(os.path.exists(approve.APPROVALS))

    def test_permission_request_distinguishes_guard_credential_and_provider(self):
        guard = agent_runtime.permission_request(
            "MAESTRO GUARD: blocked gated action 'external-send'.")
        credential = agent_runtime.permission_request(
            "authentication required: missing API key")
        provider = agent_runtime.permission_request(
            "permission prompt requires approval")
        self.assertEqual((guard["kind"], guard["scope"], guard["can_authorize"]),
                         ("guard", "external-send", True))
        self.assertEqual((credential["kind"], credential["can_authorize"]),
                         ("credential", False))
        self.assertEqual((provider["kind"], provider["scope"],
                          provider["can_authorize"]),
                         ("provider", "provider-tools", True))
        for text in (
                "Not logged in; run codex login",
                "login required",
                "Please sign in again to continue"):
            with self.subTest(text=text):
                request = agent_runtime.permission_request(text)
                self.assertEqual(request["kind"], "credential")
                self.assertFalse(request["can_authorize"])
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "permission")

    def test_success_permission_classifier_requires_unresolved_language(self):
        unresolved = (
            "I need your permission to continue",
            "Please approve this command so I can proceed",
            "Waiting for your approval before I continue",
            "I cannot proceed until you allow this tool",
            "The task requires operator approval",
            "Permission required to continue.",
            "Approval required to proceed.",
            "Operator permission required.",
            "This command needs approval before I can continue.",
            "Can you approve this command so I can continue?",
            "I am blocked pending your permission.",
        )
        for text in unresolved:
            with self.subTest(text=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "permission")
        self.assertEqual(agent_runtime.classify_failure(
            "Fixed the permission denied handling and tests pass", False), "success")
        quoted_and_resolved = (
            'Implemented dialog text "Please approve this command so I can proceed"; '
            "all tests pass.",
            'Added a fixture whose expected output is "I need your permission to '
            'continue." The suite passes.',
            'Tests cover the message "Waiting for your approval before I continue" '
            "and all 12 tests passed.",
            "Implemented the sign in flow and all tests pass",
            "Documented how to run codex login; checks pass",
            "Fixed the login required state and all tests pass",
            "Added a Please sign in CTA and verified it",
        )
        for text in quoted_and_resolved:
            with self.subTest(resolved=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "success")
        resolved_boundaries = (
            "The prior MAESTRO GUARD: blocked gated action 'deploy' was resolved; "
            "deployment completed and checks passed.",
            "Not logged in initially; authentication fixed and all tests pass.",
            "Not logged in initially; authentication fixed.",
            "MAESTRO GUARD: blocked gated action 'deploy' was resolved.",
        )
        for text in resolved_boundaries:
            with self.subTest(resolved_boundary=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "success")
        self.assertEqual(agent_runtime.classify_failure(
            'The agent said "I need your permission to continue." All tests pass.',
            False), "permission")
        self.assertEqual(agent_runtime.classify_failure(
            "I need permission to continue. A separate issue was resolved.", False),
            "permission")
        self.assertEqual(agent_runtime.classify_failure(
            "All tests pass, but please approve deploy so I can proceed", False),
            "permission")
        for text in (
                "Please approve this command so I can proceed. The code is "
                "implemented and all tests pass.",
                "I need your permission to deploy; implementation is done and tests pass.",
                "Waiting for your approval before I continue. The requested changes "
                "are completed.",
                "I cannot proceed until you allow this tool. The local checks passed."):
            with self.subTest(unresolved_despite_completion=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "permission")
        self.assertEqual(agent_runtime.classify_failure(
            "Tests pass. MAESTRO GUARD: blocked gated action 'deploy'.", False),
            "permission")
        self.assertEqual(agent_runtime.classify_failure(
            "NEEDS_OPERATOR: approve the deploy; local tests pass", False),
            "permission")
        for text in (
                "I need permission to continue; the task is not done",
                "I need permission to continue; nothing was implemented",
                "I need permission to continue; work is not yet completed"):
            with self.subTest(negated_completion=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "permission")
        credential_boundaries = (
            "Please provide an API key before I can continue",
            "Please provide an API key",
            "You need to log in before I can continue",
            "Run `codex login` and retry",
            "No credentials were found; please authenticate",
            "No credentials were found",
            "Authentication is required to continue",
            "Please enter your password to proceed",
            "Please enter your password",
            "I cannot continue until you sign in",
        )
        for text in credential_boundaries:
            with self.subTest(credential_boundary=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "permission")
                request = agent_runtime.permission_request(text)
                self.assertEqual(request["kind"], "credential")
                self.assertFalse(request["can_authorize"])
        for text in (
                "No permission is required.",
                "The task no longer requires approval.",
                "Nothing is awaiting approval.",
                "The worker is not waiting for your approval.",
                "We do not need your permission."):
            with self.subTest(negated_boundary=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "success")
        for text in (
                "Users can sign in and all tests pass",
                "Verified users sign in successfully",
                "The sign in flow works",
                "Sign in is implemented"):
            with self.subTest(normal_sign_in=text):
                self.assertEqual(
                    agent_runtime.classify_failure(text, False), "success")

    def test_codex_fallback_readiness_requires_cli_and_live_capacity(self):
        with mock.patch.object(ceo.shutil, "which", return_value=None), \
                mock.patch.object(
                    ceo.pulse, "get",
                    side_effect=AssertionError("missing CLI must fail closed first")):
            ready, reason = ceo._codex_fallback_status(now=1000)
        self.assertFalse(ready)
        self.assertIn("not installed", reason)

        connected = {"codex": {"email": "operator@example.test",
                                "pct": 25, "pct7d": 60}}
        with mock.patch.object(ceo.shutil, "which", return_value="codex.cmd"), \
                mock.patch.object(ceo.pulse, "get", return_value=connected):
            ready, reason = ceo._codex_fallback_status(now=1000)
        self.assertTrue(ready)
        self.assertIn("available capacity", reason)

        with mock.patch.object(ceo.shutil, "which", return_value="codex.cmd"), \
                mock.patch.object(
                    ceo.pulse, "get",
                    return_value={"codex": {"error": "not connected"}}), \
                mock.patch.object(ceo.pulse, "_cfg", return_value={}), \
                mock.patch.object(ceo.pulse, "_codex",
                                  return_value=connected["codex"]):
            ready, _reason = ceo._codex_fallback_status(now=1000)
        self.assertTrue(ready, "a newly running CLI must override a stale pulse error")

        exhausted = {"codex": {"email": "operator@example.test",
                                "pct": 20, "pct7d": 100,
                                "reset_at7d": 1100}}
        with mock.patch.object(ceo.shutil, "which", return_value="codex.cmd"), \
                mock.patch.object(ceo.pulse, "get", return_value=exhausted):
            ready, reason = ceo._codex_fallback_status(now=1000)
        self.assertFalse(ready)
        self.assertIn("weekly", reason)

        reset_elapsed = {"codex": {"email": "operator@example.test",
                                    "pct": 20, "pct7d": 100,
                                    "reset_at7d": 900}}
        with mock.patch.object(ceo.shutil, "which", return_value="codex.cmd"), \
                mock.patch.object(ceo.pulse, "get", return_value=reset_elapsed):
            ready, _reason = ceo._codex_fallback_status(now=1000)
        self.assertTrue(ready)

    def test_process_tree_kill_selects_windows_and_posix_helpers(self):
        class Proc:
            pid = 321
            killed = False

            def kill(self):
                self.killed = True

        commands = []

        def runner(argv, **_kwargs):
            commands.append(argv)
            return types.SimpleNamespace(returncode=0)

        win = Proc()
        method = agent_runtime.terminate_process_tree(win, platform="win32", runner=runner)
        self.assertEqual(method, "windows-taskkill-tree")
        self.assertEqual(commands, [["taskkill", "/PID", "321", "/T", "/F"]])
        self.assertFalse(win.killed)

        groups = []
        posix = Proc()
        method = agent_runtime.terminate_process_tree(
            posix, platform="linux", getpgid=lambda pid: pid + 10,
            killpg=lambda pgid, sig: groups.append((pgid, sig)))
        self.assertEqual(method, "posix-process-group")
        self.assertEqual(groups, [(331, signal.SIGTERM)])
        self.assertFalse(posix.killed)

    def test_recovery_prompt_refuses_permission_and_outward_decisions(self):
        prompt, reason = agent_runtime.build_recovery_prompt(
            "deploy to production", "permission denied; enter API key", 1)
        self.assertIsNone(prompt)
        self.assertIn("permission", reason)

        prompt, reason = agent_runtime.build_recovery_prompt(
            "fix the local test with api_key=super-secret-token", "assertion failed", 1)
        self.assertIsNone(prompt)
        self.assertIn("credential", reason)

    def test_recovery_preflight_blocks_common_outward_and_destructive_actions(self):
        for mission_text in (
                "post a message to Slack",
                "upload the artifact to S3",
                "run aws s3 sync build s3://release-bucket",
                "run git reset --hard"):
            with self.subTest(mission=mission_text):
                prompt, reason = agent_runtime.build_recovery_prompt(
                    mission_text, "the local step failed", 1)
                self.assertIsNone(prompt)
                self.assertIn("consequential", reason)

    def test_learned_evidence_redacts_bearer_credentials(self):
        item = role(recovery_history=[{
            "cycle": 1, "failure_class": "task", "learnable": True,
            "repair_summary": "Authorization: Bearer super-secret-token; fixed fixture ordering",
            "verification": "passed-original-rerun",
        }])
        evidence = agent_runtime.compact_recovery_evidence(item, learnable_only=True)
        self.assertNotIn("super-secret-token", evidence)
        self.assertIn("<redacted>", evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
