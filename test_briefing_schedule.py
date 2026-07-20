#!/usr/bin/env python3
"""Offline contract tests for Rune's 09:30 Daily Briefing scheduler.

The starter used here is inert: no repository scan, model call, subprocess, or
agent mission can occur.  Run with:

    python test_briefing_schedule.py
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import daily_briefing


BANGKOK = ZoneInfo("Asia/Bangkok")
UTC = dt.timezone.utc


class InertStarter:
    """Record a scheduled request and report it queued without doing work."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, *args, **kwargs):
        self.calls.append((args, dict(kwargs)))
        if self.error is not None:
            raise self.error
        requested = kwargs.get("date", args[0] if args else None)
        return {
            "id": "inert-job-%d" % len(self.calls),
            "status": "queued",
            "date": requested,
        }

    def requested_date(self, index=0):
        args, kwargs = self.calls[index]
        return kwargs.get("date", args[0] if args else None)


class BriefingScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-briefing-schedule-")
        self.store = os.path.join(self.temp.name, "state", "briefing.json")
        self.status = os.path.join(self.temp.name, "state", "briefing-schedule.json")

    def tearDown(self):
        self.temp.cleanup()

    def write_briefing(self, source_date, generated_at=None):
        source = dt.date.fromisoformat(source_date)
        generated_at = generated_at or dt.datetime.combine(
            source + dt.timedelta(days=1), dt.time(9, 31), tzinfo=BANGKOK
        ).isoformat()
        doc = {
            "version": 2,
            "source_date": source_date,
            "briefing_date": (source + dt.timedelta(days=1)).isoformat(),
            "generated_at": generated_at,
            "settings": {
                "model": "fable",
                "effort": "max",
                "repo_roots": [self.temp.name],
            },
            "batches": [{
                "id": "batch-" + source.strftime("%m%d"),
                "kind": "primary",
                "generated_at": generated_at,
                "priorities": [],
            }],
        }
        daily_briefing._atomic_write(self.store, doc)
        return doc

    def freshness(self, now):
        return daily_briefing.briefing_freshness(
            store_path=self.store,
            status_path=self.status,
            now=now,
            tz=BANGKOK,
        )

    def ensure(self, now, starter):
        return daily_briefing.ensure_scheduled_generation(
            store_path=self.store,
            status_path=self.status,
            now=now,
            tz=BANGKOK,
            starter=starter,
        )

    def check(self, now, starter):
        return daily_briefing.check_scheduled_generation(
            store_path=self.store,
            status_path=self.status,
            now=now,
            tz=BANGKOK,
            starter=starter,
        )

    def test_schedule_rolls_at_0930_bangkok_not_midnight(self):
        midnight = daily_briefing.schedule_window(
            now=dt.datetime(2026, 7, 16, 0, 1, tzinfo=BANGKOK), tz=BANGKOK)
        before = daily_briefing.schedule_window(
            now=dt.datetime(2026, 7, 16, 9, 29, 59, tzinfo=BANGKOK), tz=BANGKOK)
        exact = daily_briefing.schedule_window(
            now=dt.datetime(2026, 7, 16, 9, 30, tzinfo=BANGKOK), tz=BANGKOK)

        self.assertEqual(midnight["expected_source_date"], "2026-07-14")
        self.assertEqual(before["expected_source_date"], "2026-07-14")
        self.assertEqual(exact["expected_source_date"], "2026-07-15")
        self.assertEqual(exact["schedule_at"], "09:30")
        self.assertEqual(exact["timezone"], "Asia/Bangkok")
        due = dt.datetime.fromisoformat(exact["due_at"])
        following = dt.datetime.fromisoformat(exact["next_due_at"])
        self.assertEqual((due.hour, due.minute, due.utcoffset()),
                         (9, 30, dt.timedelta(hours=7)))
        self.assertEqual(following, due + dt.timedelta(days=1))

    def test_utc_input_is_converted_before_the_bangkok_boundary(self):
        before = daily_briefing.schedule_window(
            now=dt.datetime(2026, 7, 16, 2, 29, 59, tzinfo=UTC), tz=BANGKOK)
        exact = daily_briefing.schedule_window(
            now=dt.datetime(2026, 7, 16, 2, 30, tzinfo=UTC), tz=BANGKOK)

        self.assertEqual(before["expected_source_date"], "2026-07-14")
        self.assertEqual(exact["expected_source_date"], "2026-07-15")
        self.assertTrue(exact["due_at"].endswith("+07:00"))

    def test_freshness_distinguishes_awaiting_fresh_and_overdue(self):
        self.write_briefing("2026-07-14")
        awaiting = self.freshness(
            dt.datetime(2026, 7, 16, 9, 29, 59, tzinfo=BANGKOK))
        self.assertEqual(awaiting["state"], "awaiting_schedule")
        self.assertTrue(awaiting["fresh"])
        self.assertFalse(awaiting["due"])
        self.assertEqual(awaiting["expected_source_date"], "2026-07-14")
        self.assertEqual(awaiting["actual_source_date"], "2026-07-14")

        overdue = self.freshness(
            dt.datetime(2026, 7, 16, 9, 30, tzinfo=BANGKOK))
        self.assertEqual(overdue["state"], "overdue")
        self.assertFalse(overdue["fresh"])
        self.assertTrue(overdue["due"])
        self.assertEqual(overdue["expected_source_date"], "2026-07-15")
        self.assertEqual(overdue["actual_source_date"], "2026-07-14")

        self.write_briefing("2026-07-15")
        fresh = self.freshness(
            dt.datetime(2026, 7, 16, 9, 30, tzinfo=BANGKOK))
        self.assertEqual(fresh["state"], "fresh")
        self.assertTrue(fresh["fresh"])
        self.assertFalse(fresh["due"])
        self.assertEqual(fresh["last_attempt_status"], "success")
        self.assertTrue(fresh["last_success_at"])

    def test_missing_boot_catchup_queues_latest_due_cycle_only_once(self):
        starter = InertStarter()
        now = dt.datetime(2026, 7, 16, 8, 0, tzinfo=BANGKOK)

        self.ensure(now, starter)
        self.ensure(now, starter)

        self.assertEqual(len(starter.calls), 1)
        self.assertEqual(starter.requested_date(), "2026-07-14")
        self.assertNotEqual(starter.requested_date(), "yesterday")
        self.assertTrue(os.path.exists(self.status))
        state = self.freshness(now)
        self.assertEqual(state["state"], "generating")
        self.assertTrue(state["auto_catchup"])

    def test_check_now_fresh_snapshot_is_free_and_starts_no_model(self):
        self.write_briefing("2026-07-15")
        starter = InertStarter()
        result = self.check(
            dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK), starter)

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "current")
        self.assertFalse(result["started"])
        self.assertEqual(result["model_run"], {
            "queued": False, "will_run": False, "cost": "none",
        })
        self.assertIn("No model run was started", result["message"])
        self.assertEqual(starter.calls, [])

    def test_check_now_missing_cycle_queues_one_external_model_run(self):
        starter = InertStarter()
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)

        first = self.check(now, starter)
        second = self.check(now, starter)

        self.assertEqual(len(starter.calls), 1)
        self.assertTrue(first["ok"])
        self.assertEqual(first["action"], "queued")
        self.assertTrue(first["started"])
        self.assertEqual(first["source_date"], "2026-07-15")
        self.assertEqual(first["model_run"]["cost"],
                         "model_tokens_when_worker_runs")
        self.assertEqual(first["job"]["status"], "queued")
        self.assertEqual(second["action"], "already_running")
        self.assertFalse(second["started"])
        self.assertFalse(second["model_run"]["queued"])
        self.assertIn("No duplicate model run", second["message"])

    def test_check_now_running_claim_and_retry_window_never_duplicate(self):
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        starter = InertStarter()

        daily_briefing._atomic_write(self.status, {
            "status": "running",
            "source_date": "2026-07-15",
            "pid": os.getpid(),
            "started_at": now.isoformat(),
            "last_attempt_at": now.isoformat(),
        })
        running = self.check(now, starter)
        self.assertEqual(running["action"], "already_running")
        self.assertEqual(starter.calls, [])

        daily_briefing._atomic_write(self.status, {
            "status": "failed",
            "source_date": "2026-07-15",
            "last_attempt_at": now.isoformat(),
            "error": "provider unavailable",
            "retry_at": (now + dt.timedelta(minutes=15)).isoformat(),
        })
        retry = self.check(now, starter)
        self.assertEqual(retry["action"], "retry_wait")
        self.assertEqual(retry["model_run"]["cost"], "deferred_until_retry")
        self.assertEqual(starter.calls, [])

        os.remove(self.status)
        claim = daily_briefing._schedule_claim_lock_path(self.status)
        with daily_briefing._exclusive_lock(
                claim, "test claim", daily_briefing.SCHEDULE_CLAIM_LOCK_STALE_SECONDS):
            claimed = self.check(now, starter)
        self.assertEqual(claimed["action"], "already_running")
        self.assertEqual(starter.calls, [])

    def test_check_now_starter_failure_is_not_reported_as_success(self):
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        starter = InertStarter(RuntimeError("worker launch denied"))

        result = self.check(now, starter)

        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "starter_failed")
        self.assertFalse(result["started"])
        self.assertEqual(result["model_run"]["cost"], "deferred_until_retry")
        self.assertIn("last good briefing was kept", result["message"])
        self.assertIn("worker launch denied", result["freshness"]["last_error"])

    def test_scheduled_starter_receives_a_frozen_iso_date_and_safe_flags(self):
        starter = InertStarter()
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        self.ensure(now, starter)

        self.assertEqual(len(starter.calls), 1)
        args, kwargs = starter.calls[0]
        self.assertEqual(starter.requested_date(), "2026-07-15")
        self.assertNotEqual(starter.requested_date(), "yesterday")
        if "more" in kwargs:
            self.assertFalse(kwargs["more"])
        if "force" in kwargs:
            self.assertFalse(kwargs["force"])

    def test_failed_catchup_is_durable_and_keeps_last_good_visible(self):
        previous = self.write_briefing("2026-07-14")
        with open(self.store, "rb") as handle:
            previous_bytes = handle.read()
        starter = InertStarter(RuntimeError("planner provider offline"))
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)

        result = self.ensure(now, starter)

        self.assertIsNotNone(result)
        with open(self.store, "rb") as handle:
            self.assertEqual(handle.read(), previous_bytes)
        self.assertTrue(os.path.exists(self.status))
        state = self.freshness(now)
        self.assertEqual(state["state"], "failed_last_good")
        self.assertFalse(state["fresh"])
        self.assertTrue(state["due"])
        self.assertEqual(state["actual_source_date"], previous["source_date"])
        self.assertEqual(state["expected_source_date"], "2026-07-15")
        self.assertIn(state["last_attempt_status"], ("error", "failed"))
        self.assertIn("planner provider offline", state["last_error"])
        self.assertTrue(state["last_attempt_at"])
        self.assertTrue(state["last_success_at"])
        self.assertTrue(state["retry_at"])

        with open(self.status, encoding="utf-8") as handle:
            durable = json.load(handle)
        self.assertIn("planner provider offline", json.dumps(durable))

    def test_failed_first_attempt_is_visible_and_waits_before_retry(self):
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        self.ensure(now, InertStarter(RuntimeError("first planner outage")))

        state = self.freshness(now)
        self.assertEqual(state["state"], "failed")
        self.assertFalse(state["fresh"])
        self.assertEqual(state["last_attempt_status"], "failed")
        self.assertIn("first planner outage", state["last_error"])
        self.assertEqual(dt.datetime.fromisoformat(state["retry_at"]),
                         now + dt.timedelta(minutes=15))

    def test_fresh_snapshot_normalizes_a_dead_running_status(self):
        doc = self.write_briefing("2026-07-15")
        daily_briefing._atomic_write(self.status, {
            "status": "running",
            "source_date": "2026-07-15",
            "pid": 2147483647,
            "started_at": "2026-07-16T09:30:00+07:00",
            "last_attempt_at": "2026-07-16T09:30:00+07:00",
            "retry_at": "2026-07-16T09:45:00+07:00",
        })

        state = self.freshness(
            dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK))
        self.assertEqual(state["state"], "fresh")
        self.assertEqual(state["last_attempt_status"], "success")
        self.assertEqual(state["last_attempt_at"], doc["generated_at"])
        self.assertEqual(state["last_success_at"], doc["generated_at"])
        self.assertEqual(state["last_error"], "")
        self.assertEqual(state["retry_at"], "")

    def test_failed_worker_retry_is_measured_from_completion(self):
        started = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        completed = started + dt.timedelta(minutes=30)
        with mock.patch.object(
                daily_briefing, "_local_now",
                side_effect=[(started, BANGKOK), (completed, BANGKOK)]), \
                mock.patch.object(
                    daily_briefing, "generate",
                    side_effect=RuntimeError("provider timed out")):
            with self.assertRaisesRegex(RuntimeError, "provider timed out"):
                daily_briefing.scheduled_generate(
                    date="2026-07-15", store_path=self.store,
                    status_path=self.status, tz=BANGKOK)

        with open(self.status, encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["last_attempt_at"], completed.isoformat())
        self.assertEqual(status["finished_at"], completed.isoformat())
        self.assertEqual(dt.datetime.fromisoformat(status["retry_at"]),
                         completed + dt.timedelta(minutes=15))

    def test_dashboard_payload_exposes_authoritative_freshness_and_last_good(self):
        previous = self.write_briefing("2026-07-14")
        now = dt.datetime(2026, 7, 16, 10, 5, tzinfo=BANGKOK)
        self.ensure(now, InertStarter(RuntimeError("temporary model outage")))

        payload = daily_briefing.dashboard_payload(
            store_path=self.store,
            status_path=self.status,
            now=now,
            tz=BANGKOK,
        )

        self.assertEqual(payload["briefing"]["source_date"], previous["source_date"])
        self.assertEqual(payload["freshness"]["state"], "failed_last_good")
        self.assertEqual(payload["freshness"]["expected_source_date"], "2026-07-15")
        self.assertEqual(payload["freshness"]["actual_source_date"], "2026-07-14")
        self.assertIn("temporary model outage", payload["freshness"]["last_error"])

    def test_wrappers_use_the_same_scheduled_cli(self):
        for name in ("briefing.cmd", "loop.sh"):
            with self.subTest(wrapper=name):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                self.assertRegex(text, r"daily_briefing\.py[\"']?\s+scheduled\b")
                self.assertNotIn("generate --date yesterday", text)

    @unittest.skipUnless(os.name == "nt", "Windows process-query regression")
    def test_windows_pid_probe_is_read_only(self):
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            # A POSIX-style os.kill(pid, 0) is destructive on Windows. The
            # scheduler must use the read-only Kernel32 query instead.
            with mock.patch.object(
                    daily_briefing.os, "kill",
                    side_effect=AssertionError("destructive PID probe used")):
                self.assertTrue(daily_briefing._pid_alive(worker.pid))
            self.assertIsNone(worker.poll(), "the liveness probe stopped its worker")
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
