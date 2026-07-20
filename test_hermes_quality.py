import contextlib
import gzip
import hashlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from hermes import hermes


class HermesQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.paths = mock.patch.multiple(
            hermes,
            SOLVED=os.path.join(root, "solved.jsonl"),
            QUARANTINE=os.path.join(root, "quarantine.jsonl"),
            ARCHIVE_DIR=os.path.join(root, "archive"),
            ARCHIVE_INDEX=os.path.join(root, "archive", "index.json"),
            USAGE=os.path.join(root, "usage.json"),
        )
        self.paths.start()
        self.vault = mock.patch.object(hermes, "vault_path", return_value=None)
        self.vault.start()
        self.environment = mock.patch.dict(os.environ, {
            "HERMES_MAX_CARDS": "50",
            "HERMES_MAX_BYTES": str(256 * 1024),
            "HERMES_QUARANTINE_MAX_BYTES": str(32 * 1024),
            "HERMES_ARCHIVE_MAX_BYTES": str(256 * 1024),
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.vault.stop()
        self.paths.stop()
        self.tmp.cleanup()

    @staticmethod
    def reusable_solution(noun="callback"):
        return (
            "Root cause: the reverse proxy stripped the forwarded protocol, because the "
            "%s URL was reconstructed as HTTP. Set a trusted proxy allowlist, then run "
            "the regression test and compare the exact redirect URI. Verified the test "
            "passes in two services. Avoid trusting forwarded headers from arbitrary clients."
        ) % noun

    def note(self, problem, noun="callback", source="incident:one", now=None):
        return hermes.note_memory(
            problem, self.reusable_solution(noun),
            tags="testing,oauth,reusable", source=source, now=now,
        )

    def test_quality_is_explainable_and_accepts_a_verified_recipe(self):
        result = self.note("OAuth callback changes scheme behind a reverse proxy")

        self.assertEqual("accepted", result["outcome"])
        quality = result["quality"]
        self.assertGreaterEqual(quality["score"], quality["threshold"])
        codes = {item["code"] for item in quality["signals"]}
        self.assertTrue({
            "hard_earned_evidence", "root_cause", "reusable_recipe",
            "gotcha_or_guardrail", "cross_project_potential",
        }.issubset(codes))
        row = hermes.load()[0]
        self.assertEqual("incident:one", row["source"])
        self.assertEqual(row["first_seen"], row["last_verified"])
        self.assertEqual(1, row["reinforcement_count"])

    def test_raw_one_off_failure_is_quarantined_and_cli_remains_successful(self):
        problem = "Current deployment failed"
        solution = "2026-07-16 12:00:00 ERROR failed\n12:00:01 INFO task complete\ndone now"
        quality = hermes.evaluate_quality(problem, solution, source="loop:7")
        penalties = {item["code"] for item in quality["penalties"]}
        self.assertIn("raw_log_or_dump", penalties)
        self.assertIn("one_off_status", penalties)

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = hermes.cmd_note(problem, solution, "mission", "loop:7")
        self.assertEqual(0, code)
        self.assertEqual([], hermes.load())
        receipt = hermes._load_jsonl(hermes.QUARANTINE)[0]
        self.assertEqual("quality_below_threshold", receipt["reason_code"])
        self.assertIn("original_sha256", receipt)
        self.assertIn("quarantined", stream.getvalue())

    def test_high_quality_duplicate_merges_and_reinforces_instead_of_appending(self):
        first = self.note(
            "OAuth callback changes scheme behind a reverse proxy", now="2026-07-15T01:00:00+00:00"
        )
        second = self.note(
            "OAuth callback changes scheme behind a reverse proxy", noun="redirect callback",
            source="incident:two", now="2026-07-16T01:00:00+00:00",
        )

        self.assertEqual("accepted", first["outcome"])
        self.assertEqual("merged", second["outcome"])
        self.assertEqual(first["id"], second["duplicate"]["id"])
        rows = hermes.load()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["reinforcement_count"])
        self.assertEqual("2026-07-16T01:00:00+00:00", rows[0]["last_verified"])
        self.assertIn("incident:two", rows[0]["sources"])

    def test_low_quality_duplicate_does_not_inflate_reinforcement(self):
        accepted = self.note("OAuth callback changes scheme behind a reverse proxy")
        weak = hermes.note_memory(
            "OAuth callback changes scheme behind a reverse proxy", "fixed now",
            source="status:one",
        )

        self.assertEqual("quarantined", weak["outcome"])
        self.assertEqual("low_quality_duplicate", weak["reason_code"])
        self.assertEqual(accepted["id"], weak["duplicate"]["id"])
        self.assertEqual(1, hermes.load()[0]["reinforcement_count"])

    def test_query_returns_receipt_and_usage_does_not_rewrite_corpus(self):
        accepted = self.note("OAuth callback changes scheme behind a reverse proxy")
        with open(hermes.SOLVED, "rb") as handle:
            before = handle.read()
        queries_before = hermes._usage_state()["queries"]

        preview = hermes.query_memory("oauth proxy callback redirect", record_hits=False)
        self.assertEqual("hit", preview["decision"])
        self.assertEqual(accepted["id"], preview["hits"][0]["id"])
        self.assertEqual(64, len(preview["corpus_fingerprint"]))
        self.assertIn("lexical", preview["hits"][0]["score_components"])
        self.assertTrue(preview["hits"][0]["reasons"])
        self.assertEqual(queries_before, hermes._usage_state()["queries"])

        self.assertTrue(hermes.record_reuse([accepted["id"]]))
        with open(hermes.SOLVED, "rb") as handle:
            self.assertEqual(before, handle.read())
        after = hermes.query_memory("oauth proxy callback redirect", record_hits=False)
        self.assertEqual(preview["corpus_fingerprint"], after["corpus_fingerprint"])
        self.assertEqual(1, after["hits"][0]["reuse"]["hit_count"])
        self.assertEqual(1, hermes.storage_health()["usage"]["queries"])

    def test_ranking_has_source_diversity_and_no_popularity_boost(self):
        cases = [
            ("Cache invalidation should use versioned keys after deployment", "keys", "loop:1"),
            ("Cache stampede requires single flight around expensive loads", "stampede", "loop:2"),
            ("Cache TTL jitter prevents synchronized expiry bursts", "expiry", "loop:3"),
            ("Cache coherence needs explicit invalidation after database writes", "coherence", "review:1"),
        ]
        ids = []
        for problem, noun, source in cases:
            result = self.note(problem, noun=noun, source=source)
            self.assertEqual("accepted", result["outcome"])
            ids.append(result["id"])
        for _ in range(8):
            hermes.record_reuse([ids[-1]])

        result = hermes.query_memory("cache", limit=4, record_hits=False)
        families = [hit["source"].split(":", 1)[0] for hit in result["hits"]]
        self.assertLessEqual(families.count("loop"), hermes.MAX_PER_SOURCE)
        self.assertGreaterEqual(result["guards"]["source_suppressed"], 1)
        # Reuse is reported but explicitly absent from ranking components.
        self.assertNotIn("reuse", result["hits"][0]["score_components"])
        self.assertIn("zero ranking weight", result["ranking_policy"])

    def test_active_budget_compacts_losslessly_to_gzip_archive(self):
        os.environ["HERMES_MAX_CARDS"] = "2"
        results = [
            self.note("Proxy callback drops scheme on service alpha", "alpha",
                      now="2026-07-14T01:00:00+00:00"),
            self.note("Database retries duplicate writes on service beta", "beta",
                      now="2026-07-15T01:00:00+00:00"),
            self.note("Worker cancellation leaks handles on service gamma", "gamma",
                      now="2026-07-16T01:00:00+00:00"),
        ]
        self.assertTrue(all(item["outcome"] == "accepted" for item in results))
        self.assertEqual(2, len(hermes.load()))
        archive_path = os.path.join(hermes.ARCHIVE_DIR, "solved.jsonl.gz")
        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
            archived = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(1, len(archived))
        active_ids = {row["id"] for row in hermes.load()}
        self.assertNotIn(archived[0]["row"]["id"], active_ids)
        self.assertEqual("active_storage_budget", archived[0]["archive_reason"])

    def test_compaction_removes_only_archived_vault_mirror(self):
        os.environ["HERMES_MAX_CARDS"] = "2"
        vault = os.path.join(self.tmp.name, "vault")
        os.makedirs(vault)
        with mock.patch.object(hermes, "vault_path", return_value=vault):
            first = self.note(
                "Proxy callback drops scheme on service alpha", "alpha",
                now="2026-07-14T01:00:00+00:00",
            )
            second = self.note(
                "Database retries duplicate writes on service beta", "beta",
                now="2026-07-15T01:00:00+00:00",
            )
            before = {row["id"]: hermes.card_path(row) for row in hermes.load()}
            self.assertTrue(os.path.isfile(before[first["id"]]))
            self.assertTrue(os.path.isfile(before[second["id"]]))

            third = self.note(
                "Worker cancellation leaks handles on service gamma", "gamma",
                now="2026-07-16T01:00:00+00:00",
            )
            active = hermes.load()
            active_ids = {row["id"] for row in active}

            self.assertEqual(1, third["archived_during_compaction"])
            self.assertEqual(1, third["vault_cleanup"]["removed_count"])
            self.assertFalse(os.path.lexists(before[first["id"]]))
            self.assertIn(second["id"], active_ids)
            self.assertIn(third["id"], active_ids)
            for row in active:
                self.assertTrue(os.path.isfile(hermes.card_path(row)))

            folder = os.path.join(vault, "Rune", "Hermes")
            with open(os.path.join(folder, "_index.md"), encoding="utf-8") as handle:
                index = handle.read()
            self.assertNotIn("Proxy callback drops scheme on service alpha", index)
            self.assertIn("Database retries duplicate writes on service beta", index)
            self.assertIn("Worker cancellation leaks handles on service gamma", index)
            health = hermes.storage_health()
            self.assertEqual(2, health["vault_mirrors"]["active_count"])
            self.assertEqual(0, health["vault_mirrors"]["orphaned_count"])
            self.assertEqual(0, health["vault_mirrors"]["missing_active_count"])

    def test_archive_cap_is_hard_and_new_write_is_explicitly_diverted(self):
        os.environ["HERMES_MAX_CARDS"] = "1"
        os.environ["HERMES_ARCHIVE_MAX_BYTES"] = "1024"
        def dense(number, noun):
            noise = "".join(
                hashlib.sha256(("case-%d-piece-%d" % (number, piece)).encode()).hexdigest()
                for piece in range(12)
            )
            return (
                "Root cause: order dependence caused the failure. Use an isolated guard, "
                "then run the regression test; verified it passes. Avoid shared state. " +
                noun + noise
            )

        outcomes = []
        problems = [
            "Proxy callback corrupts the scheme after an ingress rewrite",
            "Database retry duplicates invoices after a connection timeout",
            "Worker cancellation leaks handles during graceful shutdown",
            "Websocket sequence reorders messages after reconnect",
            "Filesystem cache returns an obsolete manifest after deployment",
        ]
        for number, problem in enumerate(problems):
            outcomes.append(hermes.note_memory(
                problem, dense(number, "case-%d-" % number), tags="testing,reusable",
                source="audit:%d" % number,
                now="2026-07-%02dT01:00:00+00:00" % (10 + number),
            )["outcome"])
        health = hermes.storage_health()
        self.assertLessEqual(health["archive"]["bytes"], health["archive"]["max_bytes"])
        self.assertTrue(any(outcome in {"quarantined", "rejected"} for outcome in outcomes[2:]))
        self.assertLessEqual(health["cards"]["active_count"], 1)

    def test_quarantine_rotates_under_its_own_byte_budget(self):
        os.environ["HERMES_QUARANTINE_MAX_BYTES"] = "4096"
        for number in range(8):
            hermes.note_memory(
                "Status update for run %d" % number,
                "task complete; done now " + ("x" * 140),
                source="loop:%d" % number,
            )
        health = hermes.storage_health()
        self.assertLessEqual(
            health["quarantine"]["bytes"], health["quarantine"]["max_bytes"]
        )
        self.assertGreater(health["archive"]["quarantine_count"], 0)

    def test_health_flags_legacy_cards_below_threshold_without_migrating(self):
        legacy = {
            "id": "legacy1", "ts": "2025-01-01T00:00:00", "problem": "task done",
            "solution": "finished now", "tags": [], "source": "legacy", "stale": False,
        }
        hermes.save([legacy])
        with open(hermes.SOLVED, "rb") as handle:
            before = handle.read()
        health = hermes.storage_health()
        self.assertEqual(1, health["quality"]["below_threshold_count"])
        with open(hermes.SOLVED, "rb") as handle:
            self.assertEqual(before, handle.read())
        self.assertEqual({
            "accepted_count", "accepted_writes", "quarantined_count",
            "quarantined_writes", "rejected_writes", "merged_writes",
            "merged_reinforcements", "archived_count",
        }, set(health["outcomes"]))

    def test_json_cli_and_legacy_exit_codes_remain_stable(self):
        problem = "OAuth callback changes scheme behind a reverse proxy"
        solution = self.reusable_solution()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(0, hermes.main([
                "note", problem, solution, "--tags", "oauth,testing", "--source", "test",
                "--json",
            ]))
        note_result = json.loads(stream.getvalue())
        self.assertEqual("hermes.note", note_result["kind"])

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(0, hermes.main(["query", "oauth callback", "--json"]))
        query_result = json.loads(stream.getvalue())
        self.assertEqual("hermes.query", query_result["kind"])
        self.assertEqual("hit", query_result["decision"])

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(1, hermes.main(["query", "nonexistent-nonce-xyz", "--json"]))
        self.assertEqual("miss", json.loads(stream.getvalue())["decision"])

    def test_mid_sentence_recipe_is_recognized_without_lowering_threshold(self):
        quality = hermes.evaluate_quality(
            "HTTP planning request hangs long enough to kill the mission",
            "Make planning asynchronous: create the mission immediately and run planning on "
            "a background worker. Return the planning state first, then verify roles appear "
            "without blocking the request. This prevents request timeout failures.",
            tags="python,api", source="incident:planner",
        )
        self.assertGreaterEqual(quality["score"], quality["threshold"])
        self.assertIn("reusable_recipe", {item["code"] for item in quality["signals"]})
        self.assertNotIn("unresolved_failure", {item["code"] for item in quality["penalties"]})

    def test_retrieval_prefers_specific_refresh_card_and_rejects_two_term_collision(self):
        rows = [
            {
                "id": "mission", "ts": "2026-07-16T00:00:00+00:00",
                "problem": "Execute this operator-selected Daily Briefing priority plan with all stored mission fields",
                "solution": "pipeline=done; worker=done; status: complete", "tags": ["mission"],
                "source": "ceo:one", "stale": False,
            },
            {
                "id": "refresh", "ts": "2026-07-16T00:00:00+00:00",
                "problem": "Dashboard daily briefing stays stale instead of refreshing each day",
                "solution": "Root cause: the cached date key never changed after midnight. Replace it "
                            "with the requested local date, then regenerate only when that key changes. "
                            "Run the rollover regression test; verified yesterday and today differ.",
                "tags": ["cache", "testing"], "source": "incident:briefing", "stale": False,
            },
            {
                "id": "graph", "ts": "2026-07-16T00:00:00+00:00",
                "problem": "Microsoft Graph refresh token must be persisted or calendar auth fails",
                "solution": "Persist each rotated token after the callback and verify the next auth request.",
                "tags": ["oauth"], "source": "incident:graph", "stale": False,
            },
        ]
        hermes.save(rows)

        refresh = hermes.query_memory(
            "daily briefing does not refresh each day stale cached plan", record_hits=False
        )
        self.assertEqual("hit", refresh["decision"])
        self.assertEqual("refresh", refresh["hits"][0]["id"])
        collision = hermes.query_memory(
            "Microsoft calendar month day events", record_hits=False
        )
        self.assertEqual("miss", collision["decision"])


if __name__ == "__main__":
    unittest.main()
