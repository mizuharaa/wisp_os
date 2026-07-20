#!/usr/bin/env python3
"""Offline regressions for explicitly running one saved Daily Briefing plan.

These tests never call a model or start a worker process.  Run with:

    python test_briefing_run.py
"""
import copy
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
os.environ["RUNE_DISABLE_BOOT_RECOVERY"] = "1"
os.environ["RUNE_DISABLE_VERIFIER"] = "1"
os.environ["RUNE_DISABLE_AI_REVIEW"] = "1"
os.environ["RUNE_DISABLE_REPLAN"] = "1"

import ceo
import daily_briefing
import serve


class DormantThread:
    """Thread stand-in that records launch without executing its target."""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target = target
        self.args = args
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class FakeProcess:
    """Popen stand-in used to inspect headless launch kwargs only."""

    pid = 4242
    returncode = 0

    def __init__(self, argv):
        self.argv = list(argv)
        self.inputs = []

    def communicate(self, value=None, timeout=None):
        self.inputs.append(value)
        if self.argv and self.argv[0] == "codex":
            return ('{"type":"thread.started","thread_id":"fake-thread"}\n'
                    '{"type":"item.completed","item":{"type":"agent_message",'
                    '"text":"done"}}\n', "")
        return ('{"is_error":false,"result":"done","session_id":"fake-session"}', "")


class BriefingRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-briefing-run-")
        self.repos = os.path.join(self.temp.name, "repos")
        self.repo = os.path.join(self.repos, "alpha repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.store = os.path.join(self.temp.name, "state", "briefing.json")
        self.lock = os.path.join(self.temp.name, "state", "briefing.lock")
        self.repo_id = daily_briefing._repo_id(os.path.normpath(self.repo))
        self.batch_id = "batch-a"
        self.priority_id = "priority-a"
        self.doc = {
            "version": 2,
            "source_date": "2026-07-14",
            "settings": {
                "model": "fable",
                "effort": "max",
                "repo_roots": [self.repos],
            },
            "batches": [{
                "id": self.batch_id,
                "kind": "primary",
                "generated_at": "2026-07-15T09:30:00+07:00",
                "priorities": [{
                    "id": self.priority_id,
                    "rank": 1,
                    "repo": {"id": self.repo_id, "name": "alpha"},
                    "title": "Repair the persisted restart path",
                    "reason": "Yesterday's evidence exposed a restart regression.",
                    "outcome": "A restarted process restores its pending work.",
                    "first_move": "Write the failing restart check first.",
                    "ceo_plan": {
                        "steps": [
                            "Reproduce the stale state after restart.",
                            "Implement the smallest persistence fix.",
                            "Run the focused restart suite.",
                        ],
                        "definition_of_done": "The restart suite passes twice from clean state.",
                    },
                    "agents": [{
                        "id": "engineer",
                        "role": "Persistence engineer",
                        "icon": "code",
                        "mission": "Implement the bounded persistence repair.",
                        "deliverable": "A focused diff and restart test output.",
                        "model": "opus",
                        "effort": "high",
                        "status": "planned",
                    }, {
                        "id": "reviewer",
                        "role": "Restart reviewer",
                        "icon": "check",
                        "mission": "Verify recovery and permission boundaries.",
                        "deliverable": "A pass/fail review with evidence.",
                        "model": "sonnet",
                        "effort": "medium",
                        "status": "planned",
                    }],
                }],
            }],
        }
        daily_briefing._atomic_write(self.store, self.doc)

        self.old_cdir, self.old_adir = ceo.CDIR, ceo.ADIR
        ceo.CDIR = os.path.join(self.temp.name, "ceo")
        ceo.ADIR = os.path.join(ceo.CDIR, "archive")
        ceo.LIVE.clear()
        self.patches = [
            mock.patch.object(ceo, "emit", lambda *args, **kwargs: None),
            # Unit runs must never query or mutate the operator's live brain.
            mock.patch.object(ceo, "_recall", lambda _text: ""),
            mock.patch.object(ceo.threading, "Thread", DormantThread),
            mock.patch.object(
                ceo.delivery, "capture_git_baseline",
                lambda _workdir: {"available": False, "reason": "test fixture",
                                  "captured_at": "2026-07-16T00:00:00+00:00"}),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        ceo.LIVE.clear()
        ceo.CDIR, ceo.ADIR = self.old_cdir, self.old_adir
        self.temp.cleanup()

    def spec(self):
        return daily_briefing.execution_spec(
            self.batch_id, self.priority_id, store_path=self.store)

    def mixed_roles(self):
        daily_briefing.update_agent(
            self.batch_id, self.priority_id, "reviewer",
            model="gpt-5.6-sol", effort="max",
            store_path=self.store, lock_path=self.lock)
        spec = self.spec()
        return spec, daily_briefing.direct_execution_roles(spec["priority"])

    @staticmethod
    def handler_response(data):
        handler = object.__new__(serve.Handler)
        handler._json = lambda status, payload: (status, payload)
        return handler.api_briefing_run(data)

    @staticmethod
    def check_handler_response(data):
        handler = object.__new__(serve.Handler)
        handler._json = lambda status, payload: (status, payload)
        return handler.api_briefing_check(data)

    def test_check_endpoint_uses_202_only_when_a_model_worker_was_queued(self):
        queued = {
            "ok": True, "action": "queued", "started": True,
            "message": "queued", "model_run": {
                "queued": True, "will_run": True,
                "cost": "model_tokens_when_worker_runs",
            },
        }
        current = {
            "ok": True, "action": "current", "started": False,
            "message": "No model run was started.", "model_run": {
                "queued": False, "will_run": False, "cost": "none",
            },
        }
        with mock.patch.object(
                serve.daily_briefing, "check_scheduled_generation",
                side_effect=[queued, current]) as check:
            self.assertEqual(self.check_handler_response({}), (202, queued))
            self.assertEqual(self.check_handler_response({}), (200, current))
        self.assertEqual(check.call_count, 2)

    def test_check_endpoint_rejects_browser_generation_options(self):
        with mock.patch.object(
                serve.daily_briefing, "check_scheduled_generation") as check:
            status, payload = self.check_handler_response({"force": True})
        self.assertEqual(status, 400)
        self.assertIn("does not accept options", payload["error"])
        check.assert_not_called()

    def test_authoritative_lookup_builds_prompt_from_saved_plan_and_overrides(self):
        before = self.spec()
        daily_briefing.update_agent(
            self.batch_id, self.priority_id, "engineer",
            model="gpt-5.6-sol", effort="max",
            store_path=self.store, lock_path=self.lock)
        spec = self.spec()

        self.assertEqual(spec["priority"]["title"], self.doc["batches"][0]["priorities"][0]["title"])
        self.assertIn("Repair the persisted restart path", spec["prompt"])
        self.assertIn("Write the failing restart check first", spec["prompt"])
        self.assertIn("Run the focused restart suite", spec["prompt"])
        self.assertIn("requested model=gpt-5.6-sol effort=max", spec["prompt"])
        self.assertIn("cannot run that provider model", spec["prompt"])
        self.assertNotEqual(before["source"]["snapshot"], spec["source"]["snapshot"])

    def test_target_repo_is_resolved_from_server_settings_not_browser_input(self):
        spec = self.spec()
        self.assertEqual(spec["workdir"], os.path.normpath(os.path.realpath(self.repo)))
        self.assertEqual(spec["source"]["repo"]["id"], self.repo_id)
        self.assertIn("resolved workspace: %s" % spec["workdir"], spec["prompt"])

        outside = os.path.join(self.temp.name, "outside")
        os.makedirs(os.path.join(outside, ".git"))
        outside_id = daily_briefing._repo_id(os.path.normpath(outside))
        with self.assertRaisesRegex(daily_briefing.BriefingError, "outside configured roots"):
            daily_briefing.resolve_repo(
                outside_id, [self.repos], discoverer=lambda _roots: [outside])

    def test_duplicate_launch_reuses_the_same_persisted_mission(self):
        spec = self.spec()
        first, err = ceo.start_briefing_mission(
            spec["prompt"], spec["source"], spec["workdir"])
        self.assertIsNone(err)
        self.assertFalse(first["reused"])

        second, err = ceo.start_briefing_mission(
            spec["prompt"], spec["source"], spec["workdir"])
        self.assertIsNone(err)
        self.assertTrue(second["reused"])
        self.assertEqual(second["cid"], first["cid"])
        self.assertEqual(
            [name for name in os.listdir(ceo.CDIR) if name.endswith(".json")],
            [first["cid"] + ".json"],
        )

    def test_active_card_reuses_safe_run_after_model_change_and_skip_request(self):
        first_spec = self.spec()
        first, err = ceo.start_briefing_mission(
            first_spec["prompt"], first_spec["source"], first_spec["workdir"],
            permission_mode="safe")
        self.assertIsNone(err)

        changed_spec, roles = self.mixed_roles()
        self.assertNotEqual(changed_spec["source"]["snapshot"],
                            first_spec["source"]["snapshot"])
        second, err = ceo.start_briefing_mission(
            changed_spec["direct_prompt"], changed_spec["source"],
            changed_spec["workdir"], rerun=True, permission_mode="skip",
            roles=roles)

        self.assertIsNone(err)
        self.assertTrue(second["reused"])
        self.assertEqual(second["cid"], first["cid"])
        self.assertEqual(second["permission_mode"], "safe")
        self.assertEqual(len([name for name in os.listdir(ceo.CDIR)
                              if name.endswith(".json")]), 1)

    def test_archived_success_remains_idempotent_until_explicit_rerun(self):
        spec = self.spec()
        completed = {
            "cid": "completed1", "name": "completed briefing", "goal": "done",
            "source": spec["source"], "workdir": spec["workdir"],
            "roles": [], "route": "direct", "status": "done", "cost": 0,
            "started": "2026-07-15T10:00:00",
        }
        os.makedirs(ceo.CDIR, exist_ok=True)
        ceo._save(completed)
        self.assertIsNone(ceo.archive("completed1"))

        found = ceo.find_source_run(spec["source"])
        self.assertEqual(found["cid"], "completed1")
        self.assertTrue(found["archived"])
        self.assertFalse(found["live"])
        self.assertIsNone(ceo.find_source_run(
            spec["source"], active_only=True))

        reused, err = ceo.start_briefing_mission(
            spec["prompt"], spec["source"], spec["workdir"])
        self.assertIsNone(err)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["cid"], "completed1")

        rerun, err = ceo.start_briefing_mission(
            spec["prompt"], spec["source"], spec["workdir"], rerun=True)
        self.assertIsNone(err)
        self.assertFalse(rerun["reused"])
        self.assertNotEqual(rerun["cid"], "completed1")

    def test_unknown_or_stale_ids_do_not_create_a_mission(self):
        with self.assertRaisesRegex(KeyError, "batch not found"):
            daily_briefing.execution_spec(
                "stale-batch", self.priority_id, store_path=self.store)
        with self.assertRaisesRegex(KeyError, "priority not found"):
            daily_briefing.execution_spec(
                self.batch_id, "stale-priority", store_path=self.store)
        self.assertFalse(os.path.exists(ceo.CDIR))

    def test_source_workdir_and_safe_permission_mode_are_persisted(self):
        spec = self.spec()
        run, err = ceo.start_briefing_mission(
            spec["prompt"], spec["source"], spec["workdir"])
        self.assertIsNone(err)
        with open(ceo._path(run["cid"]), encoding="utf-8") as handle:
            saved = json.load(handle)

        self.assertEqual(saved["source"], spec["source"])
        self.assertEqual(saved["workdir"], spec["workdir"])
        self.assertTrue(saved["safe_permissions"])
        self.assertEqual(saved["route"], "delegate")
        self.assertEqual(saved["status"], "planning")
        self.assertNotIn("dangerously-skip-permissions", saved["goal"])

    def test_saved_mixed_models_are_authoritative_for_direct_skip_run(self):
        spec, roles = self.mixed_roles()
        self.assertEqual(
            [(role["id"], role["model"], role["provider"])
             for role in roles],
            [("engineer", "opus", "claude"),
             ("reviewer", "gpt-5.6-sol", "codex")],
        )
        self.assertEqual(roles[0]["depends_on"], [])
        self.assertEqual(roles[1]["depends_on"], ["engineer"])

        with mock.patch.object(ceo, "_api",
                               side_effect=AssertionError("direct run must not re-plan")):
            run, err = ceo.start_briefing_mission(
                spec["direct_prompt"], spec["source"], spec["workdir"],
                permission_mode="skip", roles=roles)
        self.assertIsNone(err)
        self.assertFalse(run["reused"])

        with open(ceo._path(run["cid"]), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["route"], "direct")
        self.assertEqual(saved["permission_mode"], "skip")
        self.assertEqual(saved["providers"], ["claude", "codex"])
        self.assertEqual([(role["model"], role["provider"])
                          for role in saved["roles"]],
                         [("opus", "claude"), ("gpt-5.6-sol", "codex")])

    def test_provider_mapping_and_permission_argv_are_exact(self):
        for model in ("haiku", "sonnet", "opus", "fable"):
            with self.subTest(model=model):
                self.assertEqual(ceo._provider_for_model(model), "claude")
                role = {"model": model, "provider": "claude", "turns": 20}
                safe = ceo._worker_argv(role, "safe", self.repo)
                skipped = ceo._worker_argv(role, "skip", self.repo)
                self.assertEqual(safe[:2], ["claude", "-p"])
                self.assertEqual(safe[safe.index("--model") + 1], model)
                self.assertNotIn("--dangerously-skip-permissions", safe)
                self.assertNotIn("--yolo", safe)
                self.assertEqual(skipped.count("--dangerously-skip-permissions"), 1)
                self.assertNotIn("--yolo", skipped)

        self.assertEqual(ceo._provider_for_model("gpt-5.6-sol"), "codex")
        codex = {"model": "gpt-5.6-sol", "provider": "codex", "turns": 80}
        safe = ceo._worker_argv(
            codex, "safe", self.repo, output_path=os.path.join(self.temp.name, "out.txt"))
        skipped = ceo._worker_argv(
            codex, "skip", self.repo, output_path=os.path.join(self.temp.name, "out.txt"))
        self.assertEqual(safe[:2], ["codex", "exec"])
        self.assertEqual(safe[safe.index("-m") + 1], "gpt-5.6-sol")
        self.assertNotIn("--yolo", safe)
        self.assertNotIn("--dangerously-skip-permissions", safe)
        self.assertEqual(skipped.count("--yolo"), 1)
        self.assertNotIn("--dangerously-skip-permissions", skipped)

    def test_permission_mode_is_authoritative_for_initial_resume_and_recovery(self):
        output_path = os.path.join(self.temp.name, "out.txt")
        variants = (
            (False, ""),
            (False, "provider-session"),
            (True, ""),
            (True, "provider-session"),
        )
        for recovery, resume_sid in variants:
            with self.subTest(provider="claude", recovery=recovery,
                              resumed=bool(resume_sid)):
                role = {"model": "opus", "provider": "claude", "turns": 20,
                        "recovery": recovery}
                safe = ceo._worker_argv(
                    role, "safe", self.repo, resume_sid=resume_sid)
                skipped = ceo._worker_argv(
                    role, "skip", self.repo, resume_sid=resume_sid)
                self.assertEqual(safe.count("--permission-mode"), 1)
                self.assertEqual(safe[safe.index("--permission-mode") + 1], "auto")
                self.assertNotIn("--dangerously-skip-permissions", safe)
                self.assertEqual(
                    skipped.count("--dangerously-skip-permissions"), 1)
                self.assertNotIn("--permission-mode", skipped)
                self.assertNotIn("--yolo", skipped)
                self.assertEqual("--resume" in safe, bool(resume_sid))
                self.assertEqual("--resume" in skipped, bool(resume_sid))

            with self.subTest(provider="codex", recovery=recovery,
                              resumed=bool(resume_sid)):
                role = {"model": "gpt-5.6-sol", "provider": "codex",
                        "turns": 20, "recovery": recovery}
                safe = ceo._worker_argv(
                    role, "safe", self.repo, resume_sid=resume_sid,
                    output_path=output_path)
                skipped = ceo._worker_argv(
                    role, "skip", self.repo, resume_sid=resume_sid,
                    output_path=output_path)
                self.assertEqual(safe.count("--sandbox"), 1)
                self.assertEqual(safe[safe.index("--sandbox") + 1],
                                 "workspace-write")
                self.assertIn('approval_policy="never"', safe)
                self.assertNotIn("--yolo", safe)
                self.assertEqual(skipped.count("--yolo"), 1)
                self.assertNotIn("--sandbox", skipped)
                self.assertFalse(any("approval_policy" in arg
                                     for arg in skipped))
                self.assertNotIn("--dangerously-skip-permissions", skipped)
                self.assertEqual("resume" in safe, bool(resume_sid))
                self.assertEqual("resume" in skipped, bool(resume_sid))

    def test_codex_execution_options_precede_resume_subcommand(self):
        role = {"model": "gpt-5.6-sol", "provider": "codex", "turns": 20}
        output_path = os.path.normpath(os.path.join(self.temp.name, "out.txt"))
        workdir = os.path.normpath(self.repo)
        common = ["codex", "exec", "--json", "-o", output_path,
                  "-m", "gpt-5.6-sol"]
        safe_policy = ["--sandbox", "workspace-write", "-c",
                       'approval_policy="never"']
        self.assertEqual(
            ceo._worker_argv(role, "safe", self.repo, output_path=output_path),
            common + safe_policy + ["-C", workdir, "-"],
        )
        self.assertEqual(
            ceo._worker_argv(role, "skip", self.repo, output_path=output_path),
            common + ["--yolo", "-C", workdir, "-"],
        )
        self.assertEqual(
            ceo._worker_argv(
                role, "safe", self.repo, resume_sid="thread-1",
                output_path=output_path),
            common + safe_policy + ["resume", "thread-1", "-"],
        )
        self.assertEqual(
            ceo._worker_argv(
                role, "skip", self.repo, resume_sid="thread-1",
                output_path=output_path),
            common + ["--yolo", "resume", "thread-1", "-"],
        )

    def test_worker_permission_policy_prefers_role_then_mission_then_legacy(self):
        self.assertEqual(
            ceo._permission_mode_for(
                {"permission_mode": "safe", "safe_permissions": True},
                {"permission_mode": "skip"}),
            "safe",
        )
        self.assertEqual(
            ceo._permission_mode_for(
                {"permission_mode": "safe", "safe_permissions": True},
                {"permission_mode": "skip", "operator_authorization": {
                    "status": "active", "kind": "provider",
                    "expires_at": "2999-01-01T00:00:00"}}),
            "skip",
        )
        self.assertEqual(
            ceo._permission_mode_for(
                {"permission_mode": "skip", "safe_permissions": True},
                {"permission_mode": "invalid"}),
            "skip",
        )
        self.assertEqual(
            ceo._permission_mode_for({"safe_permissions": False}), "skip")
        self.assertEqual(
            ceo._permission_mode_for({"safe_permissions": True}), "safe")
        self.assertEqual(ceo._permission_mode_for({}), "safe")

    def test_invalid_model_provider_and_permission_mode_fail_closed(self):
        for model in ("", "unknown", "opus --dangerously-skip-permissions"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                ceo._provider_for_model(model)
        with self.assertRaisesRegex(ValueError, "provider does not match"):
            ceo._worker_argv(
                {"model": "opus", "provider": "codex", "turns": 20},
                "safe", self.repo)
        with self.assertRaisesRegex(ValueError, "permission_mode"):
            ceo._worker_argv(
                {"model": "opus", "provider": "claude", "turns": 20},
                "bypass", self.repo)

    def test_corrupt_saved_execution_controls_and_models_are_rejected(self):
        priority = self.spec()["priority"]
        controls = (
            "argv", "command", "cwd", "depends_on", "env", "executable",
            "permission_mode", "provider", "recovery", "safe_permissions",
            "shell", "skip_permissions", "turns", "workdir",
        )
        for field in controls:
            with self.subTest(field=field):
                corrupt = copy.deepcopy(priority)
                corrupt["agents"][0][field] = ["cmd", "/c", "calc"]
                with self.assertRaisesRegex(
                        daily_briefing.BriefingError, "execution controls"):
                    daily_briefing.direct_execution_roles(corrupt)

        corrupt = copy.deepcopy(priority)
        corrupt["agents"][0]["model"] = "opus --dangerously-skip-permissions"
        with self.assertRaises((daily_briefing.BriefingError, ValueError)):
            daily_briefing.direct_execution_roles(corrupt)

    def test_prompt_metacharacters_never_become_worker_argv(self):
        priority = copy.deepcopy(self.spec()["priority"])
        dangerous_text = 'repair tests & calc.exe; --yolo "$(whoami)"'
        priority["agents"][0]["mission"] = dangerous_text
        role = daily_briefing.direct_execution_roles(priority)[0]
        argv = ceo._worker_argv(role, "safe", self.repo)
        self.assertIn(dangerous_text, role["mission"])
        self.assertFalse(any(dangerous_text in token for token in argv))

    def test_worker_launch_is_hidden_on_windows_and_detached_on_posix(self):
        role = {"id": "eng", "title": "Engineer", "mission": "local test",
                "model": "opus", "provider": "claude", "turns": 10,
                "depends_on": [], "review": False, "status": "pending"}

        for is_win in (True, False):
            captured = {}

            def fake_popen(argv, **kwargs):
                captured.update(argv=list(argv), kwargs=dict(kwargs))
                return FakeProcess(argv)

            with self.subTest(platform="windows" if is_win else "posix"), \
                    mock.patch.object(ceo, "IS_WIN", is_win), \
                    mock.patch.object(ceo.subprocess, "Popen", fake_popen), \
                    mock.patch.object(ceo.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                                      create=True), \
                    mock.patch.object(ceo.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200,
                                      create=True):
                result = ceo._worker(
                    "hidden", role, "", "", workdir=self.repo,
                    safe_permissions=True)
            self.assertFalse(result["is_error"])
            if is_win:
                self.assertEqual(captured["kwargs"]["creationflags"] & 0x08000000,
                                 0x08000000)
                self.assertEqual(captured["kwargs"]["creationflags"] & 0x10, 0)
            else:
                self.assertTrue(captured["kwargs"]["start_new_session"])
                self.assertEqual(captured["kwargs"]["creationflags"], 0)

    def test_endpoint_rejects_forged_fields_and_invalid_permission_modes(self):
        base = {"batch_id": self.batch_id, "priority_id": self.priority_id,
                "rerun": False, "permission_mode": "safe"}
        forged = ("model", "provider", "prompt", "repo", "command", "argv",
                  "workdir", "skip_permissions")
        with mock.patch.object(serve.daily_briefing, "execution_spec") as lookup, \
                mock.patch.object(serve.ceo, "start_briefing_mission") as starter:
            for field in forged:
                with self.subTest(field=field):
                    status, _payload = self.handler_response(
                        dict(base, **{field: "forged"}))
                    self.assertEqual(status, 400)
            for value in ("", "bypass", "SKIP", True, 1, None):
                with self.subTest(permission_mode=value):
                    status, _payload = self.handler_response(
                        dict(base, permission_mode=value))
                    self.assertEqual(status, 400)
            lookup.assert_not_called()
            starter.assert_not_called()

    def test_endpoint_derives_skip_roles_from_authoritative_snapshot(self):
        spec, roles = self.mixed_roles()
        launched = {"cid": "run1", "kind": "mission", "status": "running",
                    "source": spec["source"], "workdir": spec["workdir"],
                    "reused": False, "permission_mode": "skip"}
        with mock.patch.object(serve.daily_briefing, "execution_spec",
                               return_value=spec), \
                mock.patch.object(serve.daily_briefing, "direct_execution_roles",
                                  return_value=roles) as derive, \
                mock.patch.object(serve.ceo, "start_briefing_mission",
                                  return_value=(launched, None)) as starter, \
                mock.patch.object(serve, "emit", lambda **kwargs: None):
            status, payload = self.handler_response({
                "batch_id": self.batch_id, "priority_id": self.priority_id,
                "rerun": False, "permission_mode": "skip"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["cid"], "run1")
        derive.assert_called_once_with(spec["priority"])
        args, kwargs = starter.call_args
        self.assertEqual(args[0], spec["direct_prompt"])
        self.assertEqual(kwargs["permission_mode"], "skip")
        self.assertIs(kwargs["roles"], roles)

    def test_frontend_posts_only_ids_rerun_and_permission_mode(self):
        path = os.path.join(ROOT, "dashboard", "index.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()

        call = re.search(
            r'post\("/api/briefing/run",\{(?P<body>[^}]*)\}\)', html)
        self.assertIsNotNone(call, "Daily Briefing run endpoint is not wired")
        fields = set()
        for item in call.group("body").split(","):
            item = item.strip()
            fields.add(item.split(":", 1)[0].strip())
        self.assertEqual(fields,
                         {"batch_id", "priority_id", "rerun", "permission_mode"})
        self.assertIn('permissionMode="safe"', html)
        self.assertIn('permissionMode=permissionMode==="skip"?"skip":"safe"', html)
        self.assertIn('data-brief-permission-mode="safe"', html)
        self.assertIn('data-brief-permission-mode="skip"', html)
        self.assertIn('.btn.line.plan-run-skip', html)
        self.assertIn(
            'runButton.dataset.briefPermissionMode==="skip"?"skip":"safe"',
            html)
        self.assertIn("--dangerously-skip-permissions", html)
        self.assertIn("--yolo", html)

        start = html.index("function priorityCard(task,index)")
        end = html.index("function renderBriefing()", start)
        card = html[start:end]
        self.assertIn('data-brief-run="${esc(task.key)}"', card)
        detail = card.index('class="priority-detail"')
        insertion = card.index("${agents}${runbar}")
        self.assertGreater(insertion, detail)

    def test_frontend_ensure_current_uses_truthful_deduplicated_check(self):
        path = os.path.join(ROOT, "dashboard", "index.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()

        self.assertIn('<span>Ensure current</span>', html)
        self.assertIn('post("/api/briefing/check",{})', html)
        start = html.index("function briefingCheckDecision(result)")
        end = html.index('$("brief-generate").addEventListener', start)
        handler = html[start:end]
        for action in ("queued", "current", "already_running", "retry_wait",
                       "starter_failed"):
            self.assertIn('action==="%s"' % action, handler)
        self.assertIn("BRIEF.checking=true", handler)
        self.assertIn("await refreshBriefingData({force:true})", handler)
        self.assertNotIn("no model run will start", handler.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
