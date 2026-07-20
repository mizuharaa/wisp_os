#!/usr/bin/env python3
"""Offline integration checks for the public brain proof paths."""
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock


ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))
os.environ["RUNE_DISABLE_BOOT_RECOVERY"] = "1"
os.environ["RUNE_DISABLE_VERIFIER"] = "1"
os.environ["RUNE_DISABLE_AI_REVIEW"] = "1"
os.environ["RUNE_DISABLE_REPLAN"] = "1"

import chat
import serve


def recall_bundle(cid="chat-1", route="dashboard_chat"):
    receipt = {
        "receipt_id": "proof-1", "cid": cid, "route": route,
        "outcome": "hit", "context_chars": 48,
        "injected_prompt_count": 0, "injected_chars": 0,
        "injected_tokens_estimate": 0,
    }
    return {
        "context": "card evidence",
        "prompt_block": "\n\n## Brain recall proof marker\ncard evidence",
        "receipt": receipt,
    }


def mark_exposure(receipt, **_kwargs):
    receipt["injected_prompt_count"] = 1
    receipt["injected_chars"] = receipt["context_chars"]
    receipt["injected_tokens_estimate"] = 12
    return receipt


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class BrainApiTests(unittest.TestCase):
    def test_dashboard_chat_inserts_selected_context_before_api_send(self):
        observed = {}

        def urlopen(request, timeout=None):
            observed["timeout"] = timeout
            observed["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(json.dumps({
                "content": [{"type": "text", "text": "done"}],
            }).encode("utf-8"))

        with mock.patch.object(chat, "_api_key", return_value="test-key"), \
                mock.patch.object(chat.recall_engine, "query",
                                  return_value=recall_bundle()), \
                mock.patch.object(chat.recall_engine, "mark_exposure",
                                  side_effect=mark_exposure) as marked, \
                mock.patch.object(chat.urllib.request, "urlopen",
                                  side_effect=urlopen):
            result = chat.ask(
                "How should the cache migration be fixed?",
                recall_context={"cid": "chat-1", "route": "answer"})

        self.assertEqual("done", result["reply"])
        self.assertIn("Brain recall proof marker", observed["body"]["system"])
        self.assertNotIn("recent solved problems", observed["body"]["system"].lower())
        self.assertEqual(1, result["recall_receipt"]["injected_prompt_count"])
        marked.assert_called_once()

    def test_brain_payload_separates_observed_proof_from_unknown_savings(self):
        receipt_doc = {
            "summary": {"attempts": 1, "hits": 1},
            "receipts": [recall_bundle()["receipt"]],
        }
        health = {
            "schema_version": 2, "kind": "hermes.storage_health",
            "status": "ok", "cards": {"active_count": 3},
        }
        with mock.patch("memory.recall_engine.read_receipts",
                        return_value=receipt_doc), \
                mock.patch("hermes.hermes.storage_health", return_value=health):
            payload = serve.brain_payload()

        self.assertEqual(1, payload["schema_version"])
        self.assertEqual(1, payload["summary"]["attempts"])
        self.assertEqual(health, payload["storage"])
        self.assertEqual("retrieval_and_prompt_insertion",
                         payload["proof"]["boundary"])
        self.assertFalse(payload["proof"]["exact_savings_known"])
        self.assertFalse(payload["proof"]["model_use_proven"])

    def test_verify_endpoint_uses_production_adapter_without_model_injection(self):
        handler = object.__new__(serve.Handler)
        handler._json = lambda code, value: (code, value)
        bundle = recall_bundle(cid="verify-1", route="brain_verify")
        with mock.patch("memory.recall_engine.query", return_value=bundle) as queried:
            code, payload = handler.api_brain_query({"text": "cache migration"})

        self.assertEqual(200, code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["proof"]["model_called"])
        self.assertFalse(payload["proof"]["context_injected"])
        self.assertEqual(0, queried.call_args.kwargs["injected_prompt_count"])
        self.assertEqual("verification_only", queried.call_args.kwargs["injected_into"])

    def test_claude_hook_marks_exposure_immediately_before_printing(self):
        path = os.path.join(ROOT, ".claude", "hooks", "recall.py")
        spec = importlib.util.spec_from_file_location("brain_recall_hook_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        got = recall_bundle(cid="session-1", route="claude_hook")
        stdin = io.StringIO(json.dumps({
            "prompt": "fix the cache migration", "session_id": "session-1",
        }))
        output = io.StringIO()
        with mock.patch.object(module.sys, "stdin", stdin), \
                mock.patch.object(module.recall_engine, "query", return_value=got), \
                mock.patch.object(module.recall_engine, "mark_exposure",
                                  side_effect=mark_exposure) as marked, \
                mock.patch.dict(os.environ, {
                    "MAESTRO_BRAIN_PREINJECTED": "0",
                    "MAESTRO_SKIP_BRAIN_RECALL": "0",
                }), redirect_stdout(output):
            self.assertEqual(0, module.main())

        marked.assert_called_once()
        self.assertIn("Brain recall proof marker", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
