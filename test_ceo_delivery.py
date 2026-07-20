#!/usr/bin/env python3
"""Offline regressions for completed-mission review/test/commit/push delivery."""
import json
import os
import subprocess
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
import delivery
import serve


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-delivery-")
        self.repo = os.path.join(self.temp.name, "repo")
        os.makedirs(self.repo)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "rune-tests@example.invalid")
        self.git("config", "user.name", "Rune Tests")
        self.write("app.py", "VALUE = 1\n")
        self.write(
            "test_app.py",
            "import unittest\nimport app\n\n"
            "class AppTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertGreater(app.VALUE, 0)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n")
        self.git("add", "app.py", "test_app.py")
        self.git("commit", "-m", "initial")
        self.baseline = delivery.capture_git_baseline(self.repo)
        self.assertTrue(self.baseline["available"])
        self.assertTrue(self.baseline["clean"])

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, check=True):
        result = subprocess.run(
            ["git", "-C", self.repo] + list(args), capture_output=True,
            text=True, encoding="utf-8", check=False)
        if check and result.returncode:
            self.fail("git %s failed: %s" % (" ".join(args), result.stderr))
        return result

    def write(self, relative, value):
        with open(os.path.join(self.repo, relative), "w", encoding="utf-8") as handle:
            handle.write(value)

    def mission(self, baseline=None, cid="m1"):
        return {"cid": cid, "name": "update app value", "status": "done",
                "workdir": self.repo, "git_baseline": baseline or self.baseline,
                "roles": [], "started": "2026-07-16T10:00:00"}

    def changed_mission(self):
        self.write("app.py", "VALUE = 2\n")
        mission = self.mission()
        delivery.initialize_completed_delivery(mission)
        return mission

    def review_and_test(self, mission):
        reviewed = delivery.perform(mission, "review")
        self.assertEqual(reviewed["delivery"]["review"]["status"], "reviewed")
        tested = delivery.perform(mission, "test")
        self.assertEqual(tested["delivery"]["tests"]["status"], "passed")

    def test_review_test_and_commit_are_sequential_and_attributed(self):
        mission = self.changed_mission()
        reviewed = delivery.perform(mission, "review")["delivery"]
        self.assertIn("app.py", reviewed["review"]["report"])
        self.assertEqual(reviewed["tests"]["status"], "pending")

        tested = delivery.perform(mission, "test")["delivery"]
        self.assertEqual(tested["tests"]["status"], "passed")
        self.assertIn("unittest", " ".join(tested["tests"]["argv"]))

        committed = delivery.perform(
            mission, "commit", message="fix: update app value")["delivery"]
        self.assertEqual(committed["commit"]["status"], "committed")
        self.assertNotEqual(committed["commit"]["head"], self.baseline["head"])
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("log", "-1", "--pretty=%s").stdout.strip(),
                         "fix: update app value")

    def test_nested_pytest_project_is_detected_from_monorepo_root(self):
        with tempfile.TemporaryDirectory(prefix="rune-monorepo-") as repo:
            project = os.path.join(repo, "apps", "api")
            tests = os.path.join(project, "tests")
            source = os.path.join(project, "src")
            os.makedirs(tests)
            os.makedirs(source)

            def put(path, value):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(value)

            put(os.path.join(project, "pyproject.toml"),
                "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
            put(os.path.join(source, "__init__.py"), "")
            put(os.path.join(source, "value.py"), "VALUE = 7\n")
            put(os.path.join(tests, "__init__.py"), "")
            put(os.path.join(tests, "test_value.py"),
                "from src.value import VALUE\n\n"
                "def test_value():\n    assert VALUE == 7\n")

            argv = delivery.detect_test_argv(repo)
            self.assertEqual(argv[:5],
                             [sys.executable, "-m", "pytest", "-q", "--"])
            self.assertEqual(argv[5], "apps/api/tests")
            result = delivery._run(
                argv, repo, timeout=30,
                env={"PYTHONDONTWRITEBYTECODE": "1",
                     "PYTEST_ADDOPTS": "-p no:cacheprovider"})
            self.assertEqual(result["returncode"], 0, result["stderr"])

    def test_multiple_nested_pytest_projects_are_not_guessed(self):
        with tempfile.TemporaryDirectory(prefix="rune-monorepo-") as repo:
            for name in ("api", "worker"):
                project = os.path.join(repo, "apps", name)
                tests = os.path.join(project, "tests")
                os.makedirs(tests)
                with open(os.path.join(project, "pyproject.toml"), "w",
                          encoding="utf-8") as handle:
                    handle.write("[tool.pytest.ini_options]\n")
                with open(os.path.join(tests, "test_unit.py"), "w",
                          encoding="utf-8") as handle:
                    handle.write("def test_unit():\n    assert True\n")

            with self.assertRaisesRegex(
                    delivery.DeliveryError, "multiple nested pytest projects"):
                delivery.detect_test_argv(repo)

    def test_poetry_project_uses_its_verified_existing_environment(self):
        with tempfile.TemporaryDirectory(prefix="rune-monorepo-") as repo:
            project = os.path.join(repo, "apps", "api")
            tests = os.path.join(project, "tests")
            os.makedirs(tests)
            with open(os.path.join(project, "pyproject.toml"), "w",
                      encoding="utf-8") as handle:
                handle.write("[tool.poetry]\nname = 'api'\n"
                             "[tool.pytest.ini_options]\n")
            with open(os.path.join(tests, "test_unit.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("def test_unit():\n    assert True\n")
            interpreter = os.path.join(repo, "trusted-venv", "python.exe")

            with mock.patch.object(
                    delivery, "_poetry_test_python",
                    return_value=interpreter) as resolve:
                argv = delivery.detect_test_argv(repo)

            resolve.assert_called_once_with(repo, "apps/api")
            self.assertEqual(
                argv, [interpreter, "-m", "pytest", "-q", "--",
                       "apps/api/tests"])

    def test_poetry_environment_must_be_a_pytest_capable_virtualenv(self):
        with tempfile.TemporaryDirectory(prefix="rune-venv-") as root:
            scripts = os.path.join(root, "Scripts")
            os.makedirs(scripts)
            interpreter = os.path.join(scripts, "python.exe")
            self.write_file(interpreter, "placeholder")
            self.write_file(os.path.join(root, "pyvenv.cfg"), "home = test\n")
            successful = [
                {"returncode": 0, "stdout": interpreter + "\n", "stderr": ""},
                {"returncode": 0, "stdout": "", "stderr": ""},
            ]
            with mock.patch.object(delivery.shutil, "which",
                                   return_value="C:/tools/poetry.exe"), \
                    mock.patch.object(delivery, "_run",
                                      side_effect=successful) as run:
                resolved = delivery._poetry_test_python(root, "apps/api")

            self.assertEqual(resolved, os.path.realpath(interpreter))
            self.assertEqual(run.call_count, 2)

    @staticmethod
    def write_file(path, value):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)

    def test_test_detection_failure_is_persisted_on_the_mission(self):
        mission = self.changed_mission()
        delivery.perform(mission, "review")
        with mock.patch.object(
                delivery, "detect_test_argv",
                side_effect=delivery.DeliveryError("no trusted test scope")):
            with self.assertRaisesRegex(delivery.DeliveryError,
                                        "no trusted test scope"):
                delivery.perform(mission, "test")

        failed = mission["delivery"]
        self.assertEqual(failed["status"], "tests_unavailable")
        self.assertEqual(failed["tests"]["status"], "unavailable")
        self.assertEqual(failed["tests"]["detail"], "no trusted test scope")
        self.assertEqual(failed["tests"]["error"], "no trusted test scope")
        self.assertEqual(failed["commit"]["status"], "pending")

    def test_clean_legacy_mission_is_verification_only(self):
        bare = os.path.join(self.temp.name, "legacy-origin.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True, text=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "--set-upstream", "origin", "main")
        legacy = self.mission(dict(self.baseline, legacy=True), cid="legacy-clean")
        initialized = delivery.initialize_completed_delivery(legacy)
        self.assertFalse(initialized["changed"])
        self.assertTrue(initialized["verification_only"])
        self.assertEqual(initialized["review"]["status"], "pending")
        self.assertEqual(initialized["tests"]["status"], "pending")
        self.assertEqual(initialized["commit"]["status"], "not_needed")
        self.assertEqual(initialized["push"]["status"], "not_needed")
        self.assertNotIn("blocked_reason", initialized)

        delivery.perform(legacy, "review")
        tested = delivery.perform(legacy, "test")["delivery"]
        self.assertEqual(tested["tests"]["status"], "passed")
        self.assertEqual(tested["commit"]["status"], "not_needed")
        self.assertEqual(tested["push"]["status"], "not_needed")
        with self.assertRaisesRegex(delivery.DeliveryError,
                                    "before Git attribution"):
            delivery.perform(legacy, "commit", message="unsafe")

    def test_unpublished_clean_legacy_head_remains_attribution_blocked(self):
        legacy = self.mission(dict(self.baseline, legacy=True), cid="legacy-local")
        initialized = delivery.initialize_completed_delivery(legacy)

        self.assertTrue(initialized["changed"])
        self.assertEqual(initialized["commit"]["status"], "pending")
        self.assertIn("before Git attribution",
                      initialized["commit"]["blocked_reason"])

    def test_persisted_clean_published_legacy_lane_is_migrated(self):
        bare = os.path.join(self.temp.name, "migrated-origin.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True, text=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "--set-upstream", "origin", "main")
        legacy = self.mission(dict(self.baseline, legacy=True), cid="legacy-saved")
        legacy["delivery"] = {
            "version": 1, "changed": True, "status": "reviewed",
            "review": {"status": "reviewed"}, "tests": {"status": "pending"},
            "commit": {"status": "pending", "blocked_reason":
                       "This mission finished before Git attribution was captured."},
            "push": {"status": "pending"},
            "blocked_reason":
                "This mission finished before Git attribution was captured.",
        }

        migrated = delivery.initialize_completed_delivery(legacy)

        self.assertFalse(migrated["changed"])
        self.assertTrue(migrated["verification_only"])
        self.assertEqual(migrated["review"]["status"], "reviewed")
        self.assertEqual(migrated["tests"]["status"], "pending")
        self.assertEqual(migrated["commit"]["status"], "not_needed")
        self.assertEqual(migrated["push"]["status"], "not_needed")
        self.assertNotIn("blocked_reason", migrated)

    def test_worktree_change_invalidates_review_and_tests(self):
        mission = self.changed_mission()
        delivery.perform(mission, "review")
        self.write("app.py", "VALUE = 3\n")
        with self.assertRaisesRegex(delivery.DeliveryError, "changed after review"):
            delivery.perform(mission, "test")

    def test_stale_review_is_persisted_and_recoverable(self):
        mission = self.changed_mission()
        delivery.perform(mission, "review")
        self.write("app.py", "VALUE = 3\n")
        with self.assertRaisesRegex(delivery.DeliveryError, "changed after review"):
            delivery.perform(mission, "test")

        stale = mission["delivery"]
        self.assertEqual(stale["review"]["status"], "stale")
        self.assertEqual(stale["status"], "needs_review")
        self.assertIn("Review again", stale["review"]["detail"])
        self.review_and_test(mission)  # a fresh review unblocks the lane

    def test_unrelated_churn_never_wedges_or_rides_into_delivery(self):
        mission = self.changed_mission()
        delivery.perform(mission, "review")
        self.write("noise.log", "server appended this after the review\n")
        tested = delivery.perform(mission, "test")["delivery"]
        self.assertEqual(tested["tests"]["status"], "passed")

        self.write("noise.log", "and again before the commit\n")
        committed = delivery.perform(
            mission, "commit", message="fix: reviewed change only")["delivery"]
        self.assertEqual(committed["commit"]["status"], "committed")
        self.assertEqual(committed["commit"]["paths"], ["app.py"])
        self.assertIn("?? noise.log",
                      self.git("status", "--porcelain").stdout)

        bare = os.path.join(self.temp.name, "churn-remote.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True, text=True)
        self.git("remote", "add", "origin", bare)
        prepared = delivery.perform(mission, "prepare_push")
        pushed = delivery.perform(
            mission, "confirm_push",
            token=prepared["confirmation"]["token"])["delivery"]
        self.assertEqual(pushed["push"]["status"], "pushed")

    def test_pinned_test_command_beats_detection(self):
        self.write(".rune-test.json", json.dumps(
            {"argv": [sys.executable, "-c", "print('pinned suite ok')"]}))
        self.git("add", ".rune-test.json")
        self.git("commit", "-m", "pin test command")
        mission = self.changed_mission()
        delivery.perform(mission, "review")
        tested = delivery.perform(mission, "test")["delivery"]
        self.assertEqual(tested["tests"]["status"], "passed")
        self.assertEqual(tested["tests"]["argv"][1:], ["-c", "print('pinned suite ok')"])

        self.write(".rune-test.json", json.dumps(
            {"argv": ["pytest"], "cwd": "../outside"}))
        with self.assertRaisesRegex(delivery.DeliveryError, "cwd must stay inside"):
            delivery._override_test_argv(self.repo)

    def test_large_file_change_beyond_report_limit_invalidates_review(self):
        self.write("large.txt", "a" * 20_000 + "\n")
        mission = self.mission()
        delivery.initialize_completed_delivery(mission)
        delivery.perform(mission, "review")
        self.write("large.txt", "a" * 19_000 + "changed\n")
        with self.assertRaisesRegex(delivery.DeliveryError, "changed after review"):
            delivery.perform(mission, "test")

    def test_dirty_baseline_excludes_preexisting_work_from_commit(self):
        self.write("preexisting.txt", "user work\n")
        dirty = delivery.capture_git_baseline(self.repo)
        self.assertFalse(dirty["clean"])
        self.write("app.py", "VALUE = 4\n")
        mission = self.mission(dirty)
        delivery.initialize_completed_delivery(mission)
        self.assertNotIn("blocked_reason", mission["delivery"])
        self.review_and_test(mission)
        review = mission["delivery"]["review"]
        self.assertEqual(review["preexisting_paths"], ["preexisting.txt"])
        self.assertIn("PRE-EXISTING OPERATOR CHANGES", review["report"])

        committed = delivery.perform(
            mission, "commit", message="fix: attributed work only")["delivery"]
        self.assertEqual(committed["commit"]["paths"], ["app.py"])
        self.assertEqual(committed["commit"]["excluded_preexisting"],
                         ["preexisting.txt"])
        status = self.git("status", "--porcelain").stdout
        self.assertIn("?? preexisting.txt", status)  # operator WIP untouched
        self.assertNotIn("app.py", status)

    def test_commit_blocked_when_every_reviewed_change_is_preexisting(self):
        self.write("preexisting.txt", "user work\n")
        dirty = delivery.capture_git_baseline(self.repo)
        self.write("preexisting.txt", "user work, edited again\n")
        mission = self.mission(dirty)
        delivery.initialize_completed_delivery(mission)
        self.review_and_test(mission)
        with self.assertRaisesRegex(delivery.DeliveryError,
                                    "already dirty before the mission"):
            delivery.perform(mission, "commit", message="unsafe")
        self.assertIn("already dirty",
                      mission["delivery"]["commit"]["blocked_reason"])

    def test_stale_persisted_attribution_block_clears_on_review(self):
        self.write("preexisting.txt", "user work\n")
        dirty = delivery.capture_git_baseline(self.repo)
        self.write("app.py", "VALUE = 5\n")
        mission = self.mission(dirty)
        stale_text = "The repository was already dirty when this mission started."
        mission["delivery"] = {
            "version": 1, "available": True, "changed": True,
            "status": "needs_review", "blocked_reason": stale_text,
            "review": {"status": "pending"}, "tests": {"status": "pending"},
            "commit": {"status": "pending", "blocked_reason": stale_text},
            "push": {"status": "pending"},
        }
        self.review_and_test(mission)
        lane = mission["delivery"]
        self.assertNotIn("blocked_reason", lane)
        self.assertFalse(lane["commit"].get("blocked_reason"))
        committed = delivery.perform(
            mission, "commit", message="fix: migrated record")["delivery"]
        self.assertEqual(committed["commit"]["status"], "committed")
        self.assertEqual(committed["commit"]["paths"], ["app.py"])

    def test_legacy_baseline_blocks_automatic_commit(self):
        legacy = self.mission(dict(self.baseline, legacy=True), cid="legacy")
        delivery.initialize_completed_delivery(legacy)
        delivery.perform(legacy, "review")
        delivery.perform(legacy, "test")
        with self.assertRaisesRegex(delivery.DeliveryError, "before Git attribution"):
            delivery.perform(legacy, "commit", message="unsafe")

    def test_missing_workdir_never_attributes_the_server_repo(self):
        mission = self.mission()
        mission["workdir"] = ""
        initialized = delivery.initialize_completed_delivery(mission)
        self.assertFalse(initialized["available"])
        self.assertIn("working directory", initialized["reason"])

    def test_push_requires_fresh_one_use_token_and_never_forces(self):
        mission = self.changed_mission()
        self.review_and_test(mission)
        delivery.perform(mission, "commit", message="fix: ready to push")
        bare = os.path.join(self.temp.name, "remote.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True, text=True)
        self.git("remote", "add", "origin", bare)

        prepared = delivery.perform(mission, "prepare_push")
        confirmation = prepared["confirmation"]
        self.assertEqual(confirmation["remote"], "origin")
        self.assertEqual(confirmation["branch"], "main")
        self.assertNotIn("token_hash", json.dumps(prepared["delivery"]))

        with self.assertRaisesRegex(delivery.DeliveryError, "invalid or already used"):
            delivery.perform(mission, "confirm_push", token="wrong")
        prepared = delivery.perform(mission, "prepare_push")
        token = prepared["confirmation"]["token"]
        pushed = delivery.perform(mission, "confirm_push", token=token)["delivery"]
        self.assertEqual(pushed["push"]["status"], "pushed")
        self.assertTrue(self.git("--git-dir", bare, "show-ref", "refs/heads/main").stdout)
        with self.assertRaisesRegex(delivery.DeliveryError, "invalid or already used"):
            delivery.perform(mission, "confirm_push", token=token)

    def test_commit_hooks_remain_enabled_and_failure_is_visible(self):
        mission = self.changed_mission()
        self.review_and_test(mission)
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\necho guarded-hook-rejected >&2\nexit 1\n")
        os.chmod(hook, 0o755)

        with self.assertRaisesRegex(delivery.DeliveryError, "git commit failed"):
            delivery.perform(mission, "commit", message="must run hooks")
        self.assertEqual(mission["delivery"]["commit"]["status"], "failed")
        self.assertIn("guarded-hook-rejected",
                      mission["delivery"]["commit"]["error"])

    def test_remote_rejection_is_visible_and_consumes_confirmation(self):
        mission = self.changed_mission()
        self.review_and_test(mission)
        delivery.perform(mission, "commit", message="fix: rejected by remote")
        bare = os.path.join(self.temp.name, "rejecting.git")
        subprocess.run(["git", "init", "--bare", bare], check=True,
                       capture_output=True, text=True)
        hook = os.path.join(bare, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\necho protected-remote >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        self.git("remote", "add", "origin", bare)
        prepared = delivery.perform(mission, "prepare_push")

        with self.assertRaisesRegex(delivery.DeliveryError, "git push failed"):
            delivery.perform(mission, "confirm_push",
                             token=prepared["confirmation"]["token"])
        self.assertEqual(mission["delivery"]["push"]["status"], "failed")
        self.assertNotIn("token_hash", mission["delivery"]["push"])

    def test_public_delivery_redacts_token_and_reports_redact_credentials(self):
        mission = self.changed_mission()
        self.write("app.py", 'API_KEY="super-secret-value"\nVALUE = 2\n')
        reviewed = delivery.perform(mission, "review")["delivery"]
        self.assertNotIn("super-secret-value", reviewed["review"]["report"])
        mission["delivery"]["push"] = {"status": "awaiting_confirmation",
                                         "token_hash": "secret hash"}
        self.assertNotIn("token_hash", delivery.public_delivery(
            mission["delivery"])["push"])

    def test_review_includes_untracked_source_before_commit(self):
        mission = self.changed_mission()
        self.write("new_feature.py", "FEATURE_FLAG = True\n")
        reviewed = delivery.perform(mission, "review")["delivery"]
        self.assertIn("UNTRACKED FILE new_feature.py", reviewed["review"]["report"])
        self.assertIn("+FEATURE_FLAG = True", reviewed["review"]["report"])

    def test_ai_review_is_advisory_and_readable(self):
        mission = self.changed_mission()
        judgment = {"verdict": "attention",
                    "summary": "The diff edits app.py but ships no test update.",
                    "issues": [{"file": "app.py", "note": "value change is untested"}]}
        with mock.patch.dict(os.environ, {"RUNE_DISABLE_AI_REVIEW": "0"}), \
                mock.patch("chat._api_key", return_value="k"), \
                mock.patch("chat.structured", return_value=judgment) as call:
            reviewed = delivery.perform(mission, "review")["delivery"]

        self.assertEqual(reviewed["review"]["status"], "reviewed")  # advisory
        self.assertEqual(reviewed["review"]["ai"]["verdict"], "attention")
        self.assertIn("AI REVIEW — needs attention", reviewed["review"]["report"])
        self.assertIn("app.py: value change is untested", reviewed["review"]["report"])
        self.assertTrue(reviewed["review"]["detail"].startswith(
            "AI review: needs attention"))
        self.assertIn("MISSION GOAL", call.call_args[0][2])
        delivery.perform(mission, "test")  # the AI pass must not stale the gate

    def test_ai_review_failure_never_blocks_the_review(self):
        mission = self.changed_mission()
        with mock.patch.dict(os.environ, {"RUNE_DISABLE_AI_REVIEW": "0"}), \
                mock.patch("chat._api_key", return_value="k"), \
                mock.patch("chat.structured", return_value={"error": "API 500"}):
            reviewed = delivery.perform(mission, "review")["delivery"]
        self.assertEqual(reviewed["review"]["status"], "reviewed")
        self.assertNotIn("ai", reviewed["review"])


class MissionDeliveryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-delivery-state-")
        self.old_cdir, self.old_adir = ceo.CDIR, ceo.ADIR
        ceo.CDIR = os.path.join(self.temp.name, "active")
        ceo.ADIR = os.path.join(self.temp.name, "archive")
        os.makedirs(ceo.ADIR)
        ceo.LIVE.clear()
        ceo.DELIVERY_BUSY.clear()
        self.path = os.path.join(ceo.ADIR, "finished1.json")
        self.record = {
            "cid": "finished1", "status": "done", "workdir": self.temp.name,
            "git_baseline": {"available": True, "repo_root": self.temp.name},
            "delivery": {"version": 1, "status": "needs_review",
                         "push": {"status": "pending"}},
        }
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(self.record, handle)

    def tearDown(self):
        ceo.LIVE.clear()
        ceo.DELIVERY_BUSY.clear()
        ceo.CDIR, ceo.ADIR = self.old_cdir, self.old_adir
        self.temp.cleanup()

    def test_archived_delivery_transition_is_persisted_and_public(self):
        def perform(record, action, **_kwargs):
            self.assertEqual(action, "review")
            record["delivery"]["status"] = "reviewed"
            return {"delivery": delivery.public_delivery(record["delivery"])}

        with mock.patch.object(ceo.delivery, "perform", side_effect=perform):
            payload, error = ceo.delivery_action("finished1", "review")

        self.assertIsNone(error)
        self.assertEqual(payload["delivery"]["status"], "reviewed")
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["delivery"]["status"], "reviewed")
        self.assertTrue(os.path.isfile(self.path))
        public = ceo.public_run(dict(saved, delivery={
            "push": {"status": "awaiting_confirmation", "token_hash": "private"}}))
        self.assertNotIn("git_baseline", public)
        self.assertNotIn("token_hash", public["delivery"]["push"])

    def test_failed_confirmation_mutation_is_still_persisted(self):
        def consume_then_fail(record, _action, **_kwargs):
            record["delivery"]["push"] = {"status": "confirmation_expired"}
            raise delivery.DeliveryError("invalid or already used confirmation")

        with mock.patch.object(ceo.delivery, "perform", side_effect=consume_then_fail):
            payload, error = ceo.delivery_action(
                "finished1", "confirm_push", token="wrong")

        self.assertEqual(payload["delivery"]["push"]["status"],
                         "confirmation_expired")
        self.assertIn("invalid or already used", error)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["delivery"]["push"]["status"],
                         "confirmation_expired")

    def test_delivery_fix_spawns_a_solo_fixer_from_server_evidence(self):
        record = {
            "cid": "fixme1", "status": "done", "name": "Pipeline event API",
            "goal": "add the pipeline event API", "workdir": self.temp.name,
            "delivery": {"version": 1, "status": "tests_failed",
                         "tests": {"status": "failed",
                                    "output": "ModuleNotFoundError: no voice_daemon"}},
        }
        with open(os.path.join(ceo.ADIR, "fixme1.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(record, handle)
        calls = {}

        def fake_start(text, opts=None, source=None, workdir=None,
                       safe_permissions=False):
            calls.update(text=text, opts=opts, workdir=workdir)
            return {"cid": "fx1", "kind": "mission"}, None

        with mock.patch.object(ceo, "plan_and_start", side_effect=fake_start):
            out, err = ceo.delivery_fix("fixme1")

        self.assertIsNone(err)
        self.assertEqual(out["cid"], "fx1")
        self.assertEqual(calls["opts"]["mode"], "solo")
        self.assertEqual(calls["workdir"], self.temp.name)
        self.assertIn("delivery step 'tests'", calls["text"])
        self.assertIn("ModuleNotFoundError", calls["text"])
        self.assertIn("Do not commit", calls["text"])

    def test_delivery_fix_requires_a_failed_step(self):
        out, err = ceo.delivery_fix("finished1")
        self.assertIsNone(out)
        self.assertIn("no failed delivery step", err)

    def test_peer_delivery_in_same_repository_blocks_every_stage(self):
        peer = dict(self.record, cid="finished2")
        with open(os.path.join(ceo.ADIR, "finished2.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(peer, handle)
        ceo.DELIVERY_BUSY.add("finished2")

        with mock.patch.object(ceo.delivery, "perform") as perform:
            payload, error = ceo.delivery_action("finished1", "review")

        self.assertIsNone(payload)
        self.assertIn("using this repository", error)
        perform.assert_not_called()


class DeliveryApiTests(unittest.TestCase):
    @staticmethod
    def response(data):
        handler = object.__new__(serve.Handler)
        handler._json = lambda status, payload: (status, payload)
        return handler.api_ceo_delivery(data)

    def test_endpoint_rejects_browser_execution_controls(self):
        forged = {"cid": "mission1", "action": "review", "workdir": "C:/elsewhere"}
        status, payload = self.response(forged)
        self.assertEqual(status, 400)
        self.assertIn("unsupported", payload["error"])
        with mock.patch.object(serve.ceo, "delivery_action") as action:
            self.response(forged)
            action.assert_not_called()

    def test_endpoint_accepts_only_action_specific_minimal_payloads(self):
        with mock.patch.object(serve, "emit"), mock.patch.object(
                serve.ceo, "delivery_action",
                return_value=({"delivery": {"status": "reviewed"}}, None)) as action:
            status, _ = self.response({"cid": "mission1", "action": "review"})
            self.assertEqual(status, 200)
            action.assert_called_once_with("mission1", "review", message="", token="")

        status, _ = self.response({"cid": "mission1", "action": "confirm_push"})
        self.assertEqual(status, 400)
        status, _ = self.response({"cid": "mission1", "action": "prepare_push",
                                   "token": "forged"})
        self.assertEqual(status, 400)

    def test_endpoint_fix_action_returns_the_spawned_fixer_mission(self):
        with mock.patch.object(serve, "emit"), mock.patch.object(
                serve.ceo, "delivery_fix",
                return_value=({"cid": "fx1", "kind": "mission"}, None)) as fix:
            status, payload = self.response({"cid": "mission1", "action": "fix"})
        self.assertEqual(status, 200)
        fix.assert_called_once_with("mission1")
        self.assertEqual(payload["mission"]["cid"], "fx1")

        status, _ = self.response(
            {"cid": "mission1", "action": "fix", "message": "forged"})
        self.assertEqual(status, 400)

    def test_endpoint_returns_persisted_delivery_state_with_action_error(self):
        persisted = {"tests": {"status": "unavailable",
                                "error": "no trusted test scope"}}
        with mock.patch.object(
                serve.ceo, "delivery_action",
                return_value=({"delivery": persisted},
                              "no trusted test scope")):
            status, payload = self.response(
                {"cid": "mission1", "action": "test"})

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "no trusted test scope")
        self.assertEqual(payload["delivery"], persisted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
