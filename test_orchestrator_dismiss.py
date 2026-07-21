#!/usr/bin/env python3
"""Focused, offline regression test for the conductor-loop dismiss action.

No API key, Claude process, browser, or repository mutation is required. Run:

    python test_orchestrator_dismiss.py
"""
import json
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

import orchestrator


def loop(oid="o1", **patch):
    value = {
        "oid": oid, "name": "test loop", "mission": "m", "model": "default",
        "critic": "opus", "account": "", "turns": 40, "rounds": 3,
        "auto": True, "skip": True, "status": "done", "round": 1,
        "cost": 0, "turns_log": [], "detail": "", "next_action": "",
        "session_id": None, "started": "2026-07-20T10:00:00",
    }
    value.update(patch)
    return value


class OrchestratorDismissTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-orch-dismiss-")
        self.old_odir = orchestrator.ODIR
        orchestrator.ODIR = self.temp.name
        orchestrator.LIVE.clear()

    def tearDown(self):
        orchestrator.LIVE.clear()
        orchestrator.ODIR = self.old_odir
        self.temp.cleanup()

    def save(self, o):
        with open(os.path.join(orchestrator.ODIR, o["oid"] + ".json"),
                  "w", encoding="utf-8") as handle:
            json.dump(o, handle)
        return o

    def load(self, oid):
        with open(os.path.join(orchestrator.ODIR, oid + ".json"),
                  encoding="utf-8") as handle:
            return json.load(handle)

    def test_finished_loop_dismisses_and_drops_out_of_list_all(self):
        self.save(loop(status="done"))
        self.assertIn("o1", [o["oid"] for o in orchestrator.list_all()])

        err = orchestrator.action("o1", "dismiss")

        self.assertIsNone(err)
        self.assertTrue(self.load("o1")["dismissed"])
        self.assertNotIn("o1", [o["oid"] for o in orchestrator.list_all()])

    def test_dismiss_is_idempotent(self):
        self.save(loop(status="rejected"))
        self.assertIsNone(orchestrator.action("o1", "dismiss"))
        self.assertIsNone(orchestrator.action("o1", "dismiss"))
        self.assertTrue(self.load("o1")["dismissed"])

    def test_live_loop_refuses_dismiss(self):
        self.save(loop(status="running"))
        orchestrator.LIVE["o1"] = {
            "thread": threading.current_thread(), "proc": None,
            "stop": False, "human": None,
        }

        err = orchestrator.action("o1", "dismiss")

        self.assertIn("can't dismiss a running loop", err)
        self.assertNotIn("dismissed", self.load("o1"))
        self.assertIn("o1", [o["oid"] for o in orchestrator.list_all()])

    def test_dismiss_unknown_loop_reports_no_such_loop(self):
        self.assertEqual(orchestrator.action("missing", "dismiss"), "no such loop")


if __name__ == "__main__":
    unittest.main()
