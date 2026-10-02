from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from story_pipeline.cli import main
from story_pipeline.costs import DEFAULT_PRICES, Price, estimate, profit_sensitivity
from story_pipeline.models import JobSpec, PipelineError, digest, money_micros
from story_pipeline.providers import FixtureProvider, get_provider
from story_pipeline.roles import validate_payload
from story_pipeline.runner import Runner
from story_pipeline.store import Store


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.spec = JobSpec("episode-1", "Two adult relatives remember one decision differently.", 25, 1_000_000)
        self.store.create(self.spec)
        self.provider = FixtureProvider()
        self.runner = Runner(self.store, self.provider)
        self.network_guards = [
            patch("socket.socket", side_effect=AssertionError("Network forbidden in dry-run")),
            patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden in dry-run")),
        ]
        for guard in self.network_guards:
            guard.start()

    def tearDown(self):
        for guard in reversed(self.network_guards):
            guard.stop()
        self.store.close()
        self.temp.cleanup()

    def finish(self):
        result = self.runner.run("episode-1", dry_run=True)
        self.assertEqual(result["status"], "awaiting_script_approval")
        self.store.approve("episode-1", "script", "test-reviewer")
        result = self.runner.run("episode-1", dry_run=True)
        self.assertEqual(result["status"], "awaiting_review")
        self.store.approve("episode-1", "review", "test-reviewer")
        return self.runner.run("episode-1", dry_run=True)

    def test_complete_flow_requires_two_reviews_and_never_creates_media(self):
        result = self.finish()
        self.assertEqual(result["status"], "simulated_complete")
        self.assertFalse(result["production_ready"])
        self.assertFalse(result["publishing_available"])
        self.assertFalse(result["media_created"])
        self.assertIsNone(result["actual_duration_seconds"])
        self.assertEqual(result["costs"]["actual_external_spend_usd"], "0.000000")
        self.assertEqual(self.provider.calls, ["research", "write", "edit", "direct", "produce", "qa"])
        for stage in result["stages"]:
            artifact = self.store.read_artifact(self.store.stage("episode-1", stage["name"]))
            self.assertTrue(artifact["simulated"])
        self.assertFalse(list(self.root.rglob("*.mp4")))
        self.assertFalse(list(self.root.rglob("*.wav")))

    def test_resume_preserves_completed_steps_and_costs_after_reopening(self):
        first = self.runner.run("episode-1", dry_run=True)
        self.store.close()
        self.store = Store(self.root)
        new_provider = FixtureProvider()
        result = Runner(self.store, new_provider).run("episode-1", dry_run=True)
        self.assertEqual(new_provider.calls, [])
        self.assertEqual(first["costs"], result["costs"])
        self.assertEqual(result["status"], "awaiting_script_approval")

    def test_revision_invalidates_only_downstream_and_approval(self):
        self.finish()
        research_hash = self.store.stage("episode-1", "research")["artifact_hash"]
        previous_cost = self.store.report("episode-1")["costs"]["virtual_consumed_usd"]
        self.store.revise("episode-1", "write", "Revise the child's motivation.")
        with self.assertRaises(PipelineError):
            self.store.approved("episode-1", "script")
        self.provider.calls.clear()
        result = self.runner.run("episode-1", dry_run=True)
        self.assertEqual(result["status"], "awaiting_script_approval")
        self.assertEqual(self.provider.calls, ["write", "edit", "direct"])
        self.assertEqual(self.store.stage("episode-1", "research")["artifact_hash"], research_hash)
        self.assertNotEqual(previous_cost, result["costs"]["virtual_consumed_usd"])
        self.assertFalse(self.store.approved("episode-1", "script"))

    def test_identical_revision_does_not_reset_attempt_limit(self):
        self.store.revise("episode-1", "write", "Same instruction")
        with self.assertRaisesRegex(PipelineError, "unchanged"):
            self.store.revise("episode-1", "write", "Same instruction")

    def test_tampered_artifact_is_not_silently_regenerated(self):
        self.runner.run("episode-1", dry_run=True)
        row = self.store.stage("episode-1", "write")
        (self.root / row["artifact_path"]).write_text("{}", encoding="utf-8")
        calls = list(self.provider.calls)
        with self.assertRaisesRegex(PipelineError, "integrity"):
            self.runner.run("episode-1", dry_run=True)
        self.assertEqual(calls, self.provider.calls)
        self.assertEqual(self.store.report("episode-1")["status"], "blocked")
        with self.assertRaisesRegex(PipelineError, "integrity"):
            self.store.approve("episode-1", "script", "reviewer")

    def test_zero_budget_blocks_before_any_provider_call(self):
        spec = JobSpec("zero", "Fixture", 20, 0)
        self.store.create(spec)
        with self.assertRaisesRegex(PipelineError, "budget"):
            self.runner.run("zero", dry_run=True)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.store.report("zero")["attempts"], [])

    def test_missing_price_blocks_before_execution(self):
        runner = Runner(self.store, self.provider, {})
        with self.assertRaisesRegex(PipelineError, "Missing price"):
            runner.run("episode-1", dry_run=True)
        self.assertEqual(self.provider.calls, [])

    def test_transient_failures_are_bounded_and_retained_in_virtual_ledger(self):
        provider = FixtureProvider({"write": 4})
        runner = Runner(self.store, provider)
        with self.assertRaisesRegex(PipelineError, "attempt limit"):
            runner.run("episode-1", dry_run=True)
        self.assertEqual(provider.calls.count("write"), 2)
        report = self.store.report("episode-1")
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(report["costs"]["virtual_reserved_usd"], "0.000000")
        self.assertEqual(report["costs"]["actual_external_spend_usd"], "0.000000")
        with self.assertRaisesRegex(PipelineError, "attempt limit"):
            runner.run("episode-1", dry_run=True)
        self.assertEqual(provider.calls.count("write"), 2)

    def test_retry_can_recover_without_repeating_upstream(self):
        provider = FixtureProvider({"write": 1})
        result = Runner(self.store, provider).run("episode-1", dry_run=True)
        self.assertEqual(result["status"], "awaiting_script_approval")
        self.assertEqual(provider.calls, ["research", "write", "write", "edit", "direct"])
        self.assertEqual(self.store.stage("episode-1", "write")["attempts"], 2)

    def test_interruption_requires_explicit_recovery(self):
        quote = estimate(self.provider.usage("research", None), DEFAULT_PRICES)
        input_hash = digest({
            "spec": asdict(self.spec),
            "stage": "research",
            "instruction": "",
            "upstream_hash": None,
            "provider": self.provider.version,
            "estimate": quote,
        })
        self.store.reserve(self.spec, "research", input_hash, quote)
        with self.assertRaisesRegex(PipelineError, "--recover"):
            self.runner.run("episode-1", dry_run=True)
        result = self.runner.run("episode-1", dry_run=True, recover=True)
        self.assertEqual(result["status"], "awaiting_script_approval")
        self.assertEqual(result["attempts"][0]["status"], "interrupted")
        self.assertGreater(result["attempts"][0]["simulated_actual_micros"], 0)

    def test_live_provider_and_non_dry_run_are_rejected(self):
        with self.assertRaisesRegex(PipelineError, "Live providers"):
            get_provider("azure")
        with self.assertRaisesRegex(PipelineError, "dry-run"):
            self.runner.run("episode-1", dry_run=False)
        self.assertEqual(self.provider.calls, [])

    def test_review_cannot_be_recorded_before_artifacts_exist(self):
        with self.assertRaisesRegex(PipelineError, "no completed artifact"):
            self.store.approve("episode-1", "review", "reviewer")

    def test_worker_lock_excludes_another_store(self):
        other = Store(self.root)
        try:
            with self.store.lock():
                with self.assertRaisesRegex(PipelineError, "Another process"):
                    with other.lock():
                        self.fail("Second worker acquired the lock")
        finally:
            other.close()

    def test_changed_price_invalidates_dependent_fingerprint(self):
        self.runner.run("episode-1", dry_run=True)
        old_hash = self.store.stage("episode-1", "research")["artifact_hash"]
        prices = dict(DEFAULT_PRICES)
        prices["model_input_tokens"] = Price("Different assumption", "3", 1_000_000, "hypothetical", "test")
        Runner(self.store, prices=prices).run("episode-1", dry_run=True)
        self.assertNotEqual(old_hash, self.store.stage("episode-1", "research")["artifact_hash"])

    def test_invalid_provider_output_is_blocked(self):
        class InvalidFixture(FixtureProvider):
            def generate(self, stage, spec, previous, instruction):
                return {"simulated": True}
        with self.assertRaisesRegex(PipelineError, "missing required"):
            Runner(self.store, InvalidFixture()).run("episode-1", dry_run=True)
        self.assertEqual(self.store.stage("episode-1", "research")["status"], "failed")

    def test_artifact_path_cannot_escape_workspace(self):
        self.runner.run("episode-1", dry_run=True)
        with self.store.db:
            self.store.db.execute(
                "UPDATE stages SET artifact_path='../escape.json' WHERE job_id=? AND name='write'",
                ("episode-1",),
            )
        with self.assertRaisesRegex(PipelineError, "escapes"):
            self.runner.run("episode-1", dry_run=True)


class CostAndContractTests(unittest.TestCase):
    def test_cash_target_does_not_deduct_owner_labor(self):
        result = profit_sensitivity(170_000)
        self.assertFalse(result["owner_labor_deducted"])
        self.assertEqual(result["required_ad_revenue_krw"], 1_170_000)
        self.assertEqual(
            [row["required_monthly_catalog_views"] for row in result["scenarios"]],
            [1_170_000, 390_000, 234_000],
        )
        self.assertEqual(result["pre_ypp_creator_ad_revenue_krw"], 0)

    def test_verified_speech_price_and_assumed_image_price_are_distinct(self):
        quote = estimate({"speech_characters": 30_000}, DEFAULT_PRICES)
        self.assertEqual(quote["total_usd"], "0.450000")
        self.assertEqual(quote["items"][0]["evidence"], "public_retail")
        self.assertEqual(DEFAULT_PRICES["images"].evidence, "hypothetical")

    def test_fractional_micro_is_rounded_up_not_free(self):
        quote = estimate({"model_input_tokens": 1}, DEFAULT_PRICES)
        self.assertEqual(quote["total_micros"], 1)

    def test_money_and_usage_reject_invalid_inputs(self):
        for value in ("NaN", "Infinity", "-1", "0.0000001", "nonsense", "1000001"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                money_micros(value)
        self.assertEqual(money_micros("0.000001"), 1)
        with self.assertRaises(PipelineError):
            estimate({"images": -1}, DEFAULT_PRICES)
        with self.assertRaises(PipelineError):
            profit_sensitivity(-1)

    def test_invalid_ids_and_runtime_are_rejected(self):
        for identifier in ("../escape", "", "UPPER", "a" * 65):
            with self.subTest(identifier=identifier), self.assertRaises(PipelineError):
                JobSpec(identifier, "Fixture", 25, 1).validate()
        with self.assertRaises(PipelineError):
            JobSpec("fine", "Fixture", 19, 1).validate()

    def test_simulated_qa_cannot_assert_real_quality(self):
        payload = FixtureProvider().generate(
            "qa", JobSpec("fine", "Fixture", 25, 1), {}, ""
        )
        payload["checks"]["audio"] = "passed"
        with self.assertRaisesRegex(PipelineError, "cannot claim"):
            validate_payload("qa", payload)

    def test_script_and_scene_mapping_must_match(self):
        payload = FixtureProvider().generate(
            "write", JobSpec("fine", "Fixture", 25, 1), {}, ""
        )
        payload["scenes"][0]["text"] = "An unapproved replacement line."
        with self.assertRaisesRegex(PipelineError, "do not match"):
            validate_payload("write", payload)

    def test_role_outputs_cannot_hide_their_simulated_status(self):
        with self.assertRaisesRegex(PipelineError, "explicitly simulated"):
            validate_payload("research", {"candidates": [], "sources": [], "rights_status": "approved"})


class CliTests(unittest.TestCase):
    def test_cli_flow_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = ["--workspace", directory]
            def call(*args):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main(base + list(args))
                return code, json.loads(stdout.getvalue() or stderr.getvalue())

            code, _ = call("plan", "demo", "--concept", "Original fixture", "--simulation-budget-usd", "1")
            self.assertEqual(code, 0)
            code, result = call("run", "demo", "--dry-run")
            self.assertEqual(code, 2)
            self.assertEqual(result["status"], "awaiting_script_approval")
            code, approval = call("approve", "demo", "--kind", "script", "--reviewer", "tester")
            self.assertEqual(code, 0)
            self.assertFalse(approval["publishing_authorized"])
            self.assertEqual(call("resume", "demo", "--dry-run")[0], 2)
            self.assertEqual(call("approve", "demo", "--kind", "review", "--reviewer", "tester")[0], 0)
            self.assertEqual(call("resume", "demo", "--dry-run")[1]["status"], "simulated_complete")
            report = call("cost-report", "demo", "--monthly-cash-krw", "170000")[1]
            self.assertEqual(report["profit_sensitivity"]["required_ad_revenue_krw"], 1_170_000)
            self.assertEqual(call("run", "demo", "--dry-run", "--provider", "azure")[0], 1)
            self.assertEqual(call("status", "missing")[0], 1)

    def test_dry_run_flag_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["run", "demo"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
