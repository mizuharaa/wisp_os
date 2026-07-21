#!/usr/bin/env python3
"""Focused, offline regression test for persisting the CEO's `answer` route.

No API key, Claude process, browser, or repository mutation is required. Run:

    python test_ceo_answer_persist.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
os.environ["RUNE_DISABLE_BOOT_RECOVERY"] = "1"

import ceo


class CeoAnswerPersistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rune-ceo-answer-")
        self.old_cdir, self.old_adir = ceo.CDIR, ceo.ADIR
        ceo.CDIR = os.path.join(self.temp.name, "active")
        ceo.ADIR = os.path.join(self.temp.name, "archive")
        ceo.LIVE.clear()

    def tearDown(self):
        ceo.LIVE.clear()
        ceo.CDIR, ceo.ADIR = self.old_cdir, self.old_adir
        self.temp.cleanup()

    def test_answer_route_persists_a_minimal_done_record(self):
        stub = {"reply": "It counts soul files.", "model": "haiku",
                "recall_receipt": None}
        with mock.patch.object(ceo.chat, "ask", return_value=stub) as ask:
            out, err = ceo.plan_and_start(
                "what does X do", {"mode": "answer"})

        self.assertIsNone(err)
        self.assertEqual(out["kind"], "answer")
        self.assertTrue(out["cid"].startswith("answer-"))
        self.assertEqual(out["reply"], "It counts soul files.")
        ask.assert_called_once()

        path = os.path.join(ceo.CDIR, out["cid"] + ".json")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["route"], "answer")
        self.assertEqual(saved["roles"], [])
        self.assertEqual(saved["reply"], "It counts soul files.")

        history_cids = [run["cid"] for run in ceo.list_history()]
        self.assertIn(out["cid"], history_cids)

    def test_answer_route_error_is_not_persisted(self):
        with mock.patch.object(ceo.chat, "ask", return_value={"error": "no key"}):
            out, err = ceo.plan_and_start("what does X do", {"mode": "answer"})

        self.assertIsNone(out)
        self.assertEqual(err, "no key")
        self.assertFalse(os.path.isdir(ceo.CDIR))


if __name__ == "__main__":
    unittest.main()
