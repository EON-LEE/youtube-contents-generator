from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from story_pipeline.models import PipelineError
from story_pipeline.studio.learning import (
    FoundryAgentVersions, apply_retrospective, build_dataset, calibrate, decide_promotion, decide_rollback,
    monthly_report, retro_inputs, run_scores, write_dataset,
)
from story_pipeline.studio.playbook import Playbook


def lesson(text="장면 전환은 감정 변화에 맞춰라.", roles=("director",), confidence="low", evidence="ep1 비평"):
    return {"text": text, "roles": list(roles), "confidence": confidence, "evidence": evidence}


def make_run(root: Path, name: str, *, prompt: bool = True, script_mean: float = 7.5, final: float = 8.0) -> Path:
    run = root / name
    (run / "stages").mkdir(parents=True)
    (run / "stages" / "concept.json").write_text(json.dumps({"chosen": {"logline": "x" * 500}, "score": 7.8}),
                                                 encoding="utf-8")
    (run / "stages" / "write.json").write_text(json.dumps({"scenes": {}, "history": [
        {"round": 1, "scores": {"korean": 6.0}, "mean": 6.0},
        {"round": 2, "scores": {"korean": script_mean}, "mean": script_mean}]}), encoding="utf-8")
    (run / "stages" / "packaging.json").write_text(json.dumps({"scores": {"titles": [
        {"index": 0, "score": 6.5, "rationale": ""}, {"index": 1, "score": 8.0, "rationale": ""}]}}),
        encoding="utf-8")
    records = [
        {"stage": "write", "agent": "scene-writer", "iteration": 1, "model": "m", "agent_version": "3",
         "input_tokens": 100, "output_tokens": 50, "usd": "0.01", "output": {"id": "s1", "narration": "..."}},
        {"stage": "write", "agent": "scene-writer", "iteration": 2, "error": "timeout"},
        {"stage": "final", "agent": "final-judge", "iteration": 1, "model": "g", "agent_version": "1",
         "input_tokens": 10, "output_tokens": 5, "usd": "0.002", "output": {"approve": True, "score": final}},
    ]
    if prompt:
        for record in records:
            record["prompt"] = f"prompt for {record['agent']}"
    (run / "team-trace.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                          encoding="utf-8")
    (run / "ledger.json").write_text(json.dumps({"budget_usd": "10", "committed_usd": "1.2", "settled_usd": "1.1",
                                                 "entries": [{"status": "settled"}, {"status": "failed"}]}),
                                     encoding="utf-8")
    return run


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.playbook = Playbook(self.root / "playbook.json")
        self.existing = self.playbook.add("기존 교훈", ["all"], "low", "근거", "ep0")
        self.playbook.save()

    def tearDown(self):
        self.dir.cleanup()

    def test_adds_and_retires(self):
        output = {"lessons": [lesson(), lesson("기획은 갈등을 첫 문장에 둬라.", ["concept-writer", "all"]),
                              lesson("기존 교훈", ["all"])],
                  "retire_lesson_ids": [self.existing]}
        summary = apply_retrospective(self.playbook, output, "ep1")
        self.assertEqual(len(summary["added"]), 2)
        self.assertEqual(summary["duplicates"], [self.existing])
        self.assertEqual(summary["retired"], [self.existing])
        self.assertEqual(summary["playbook_version"], 2)
        reloaded = Playbook(self.root / "playbook.json")
        self.assertEqual(len(reloaded.active()), 2)
        self.assertEqual(len(reloaded.active("concept-writer-b")), 1)
        again = apply_retrospective(reloaded, {"lessons": [], "retire_lesson_ids": [self.existing]}, "ep2")
        self.assertEqual(again["already_retired"], [self.existing])
        self.assertEqual(again["playbook_version"], 2)

    def test_rejects_invalid_output_without_applying(self):
        cases = [
            ({"lessons": [lesson(roles=["ghost-writer"])], "retire_lesson_ids": []}, "unknown roles"),
            ({"lessons": [lesson()], "retire_lesson_ids": ["L9999"]}, "unknown lesson ids"),
            ({"lessons": [lesson(confidence="certain")], "retire_lesson_ids": []}, "not one of"),
            ({"lessons": [lesson()]}, "missing"),
            ({"lessons": [lesson(roles=[])], "retire_lesson_ids": []}, "unknown roles"),
        ]
        for output, message in cases:
            with self.assertRaisesRegex(PipelineError, message):
                apply_retrospective(self.playbook, output, "ep1")
        self.assertEqual(len(self.playbook.lessons), 1)
        self.assertEqual(Playbook(self.root / "playbook.json").version, 1)
        with self.assertRaisesRegex(PipelineError, "unknown roles"):
            apply_retrospective(self.playbook, {"lessons": [lesson()], "retire_lesson_ids": []}, "ep1",
                                allowed_roles=["all"])

    def test_retro_inputs_summarizes_run(self):
        run = make_run(self.root, "ep1")
        rows = [{"video_id": "v1", "date": "2026-10-01", "views": 10, "averageViewPercentage": 50.0}]
        inputs = retro_inputs(run, rows, ["v1"])
        self.assertEqual(inputs["agents"]["scene-writer"]["calls"], 2)
        self.assertEqual(inputs["agents"]["scene-writer"]["errors"], ["timeout"])
        self.assertEqual(inputs["agents"]["scene-writer"]["usd"], "0.01")
        self.assertEqual(inputs["scores"]["final"], 8.0)
        self.assertEqual(inputs["ledger"]["failed"], 1)
        self.assertEqual(inputs["metrics"]["v1"]["retention"], 0.5)
        self.assertLess(len(inputs["stages"]["concept"]["chosen"]["logline"]), 220)
        json.dumps(inputs, ensure_ascii=False)
        with self.assertRaisesRegex(PipelineError, "stages"):
            retro_inputs(self.root / "nothing")


class CalibrationTests(unittest.TestCase):
    def records(self, n):
        out = []
        for i in range(n):
            out.append({"episode": f"e{i}", "axis_scores": {"engagement": 5 + i * 0.5, "noise": [7, 3, 3, 7][i % 4] + 0.0,
                                                            "inverse": 9 - i * 0.5},
                        "metrics": {"retention": 0.3 + i * 0.02, "ctr": 0.05 + i * 0.001}})
        return out

    def test_too_few_samples_keep_unit_weights(self):
        report = calibrate(self.records(5))
        self.assertEqual(report["n"], 5)
        self.assertFalse(report["calibrated"])
        self.assertEqual(set(report["weights"].values()), {1.0})
        self.assertAlmostEqual(report["correlations"]["engagement"]["retention"], 1.0)

    def test_weights_follow_correlation(self):
        report = calibrate(self.records(8))
        self.assertTrue(report["calibrated"])
        self.assertEqual(report["weights"]["engagement"], 1.5)
        self.assertEqual(report["weights"]["inverse"], 0.5)
        self.assertAlmostEqual(report["correlations"]["engagement"]["ctr"], 1.0)
        self.assertIn("noise", report["not_predictive"])
        self.assertNotIn("engagement", report["not_predictive"])

    def test_constant_axis_has_no_correlation(self):
        records = [{"axis_scores": {"flat": 7}, "metrics": {"retention": 0.1 * i}} for i in range(9)]
        report = calibrate(records)
        self.assertIsNone(report["correlations"]["flat"]["retention"])
        self.assertEqual(report["weights"]["flat"], 1.0)


class OptimizeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_dataset_rows_and_files(self):
        runs = [make_run(self.root, "ep1"), make_run(self.root, "ep2", script_mean=8.2)]
        rows = build_dataset(runs)
        self.assertEqual(len(rows), 4)
        writer = rows[0]
        self.assertEqual((writer["agent"], writer["query"]), ("scene-writer", "prompt for scene-writer"))
        self.assertEqual(json.loads(writer["response"])["id"], "s1")
        self.assertEqual(writer["downstream"]["script"], 7.5)
        self.assertEqual(rows[2]["downstream"]["script"], 8.2)
        self.assertEqual(writer["downstream"]["packaging"], 8.0)
        paths = write_dataset(rows, self.root / "dataset")
        self.assertEqual(sorted(paths), ["final-judge", "scene-writer"])
        self.assertEqual(len(paths["scene-writer"].read_text(encoding="utf-8").splitlines()), 2)

    def test_dataset_requires_prompt_text(self):
        run = make_run(self.root, "ep1", prompt=False)
        with self.assertRaisesRegex(PipelineError, "no prompt text"):
            build_dataset([run])
        self.assertIsNone(build_dataset([run], allow_missing_prompt=True)[0]["query"])
        (self.root / "empty" / "stages").mkdir(parents=True)
        with self.assertRaisesRegex(PipelineError, "no team-trace"):
            build_dataset([self.root / "empty"])
        self.assertEqual(run_scores(self.root / "empty"), {"concept": None, "script": None, "script_axes": None,
                                                           "packaging": None, "final": None})

    def test_promotion(self):
        base = [{"a": 7.0, "b": 7.0}] * 5
        good = [{"a": 7.6, "b": 7.2}] * 5
        self.assertTrue(decide_promotion(base, good)["promote"])
        small = decide_promotion(base, [{"a": 7.2, "b": 7.2}] * 5)
        self.assertFalse(small["promote"])
        self.assertIn("below", small["reason"])
        regress = decide_promotion(base, [{"a": 9.0, "b": 6.4}] * 5)
        self.assertFalse(regress["promote"])
        self.assertIn("regressed", regress["reason"])
        few = decide_promotion(base[:4], good[:4])
        self.assertFalse(few["promote"])
        with self.assertRaisesRegex(PipelineError, "same held-out"):
            decide_promotion(base, good[:4])
        with self.assertRaisesRegex(PipelineError, "different axes"):
            decide_promotion(base, [{"a": 8.0}] * 5)

    def test_rollback(self):
        self.assertTrue(decide_rollback([0.30, 0.32, 0.31], [0.40, 0.38])["rollback"])
        self.assertFalse(decide_rollback([{"retention": 0.37}] * 3, [{"retention": 0.40}])["rollback"])
        self.assertTrue(decide_rollback([0.34] * 3, [0.40])["rollback"])
        self.assertFalse(decide_rollback([0.1, 0.1], [0.4])["rollback"])
        self.assertFalse(decide_rollback([0.1] * 3, [])["rollback"])
        with self.assertRaisesRegex(PipelineError, "without retention"):
            decide_rollback([{"views": 1}] * 3, [0.4])

    def test_foundry_versions(self):
        created = []

        class Agents:
            def create_version(self, **kwargs):
                created.append(kwargs)
                return SimpleNamespace(version=str(len(created) + 3))

            def get_version(self, agent_name, agent_version):
                if agent_version == "404":
                    raise RuntimeError("not found")
                return SimpleNamespace(version=agent_version, definition={"model": "old"}, metadata={"k": "v"})

            def list_versions(self, agent_name):
                return [SimpleNamespace(version=1), SimpleNamespace(version=2)]

        versions = FoundryAgentVersions(SimpleNamespace(agents=Agents()))
        self.assertEqual(versions.versions("scene-writer"), ["1", "2"])
        self.assertEqual(versions.promote("scene-writer", {"model": "new"}, {"gain": "0.4"}),
                         {"agent": "scene-writer", "version": "4"})
        result = versions.rollback("scene-writer", "2")
        self.assertEqual(result, {"agent": "scene-writer", "version": "5", "restored_from": "2"})
        self.assertEqual(created[-1]["definition"], {"model": "old"})
        self.assertEqual(created[-1]["metadata"], {"k": "v", "rollback_of": "2"})
        with self.assertRaisesRegex(PipelineError, "Cannot read"):
            versions.rollback("scene-writer", "404")


class ReportTests(unittest.TestCase):
    ledgers = [{"episode": "e1", "committed_usd": "6.5", "final_score": 7.6, "script_score": 7.1},
               {"episode": "e2", "committed_usd": "5.5", "final_score": 8.0, "script_score": 7.4},
               {"episode": "e3", "committed_usd": "4", "final_score": 8.4}]
    rows = [{"video_id": "v1", "date": "2026-10-01", "views": 6000, "estimatedMinutesWatched": 6000},
            {"video_id": "v2", "date": "2026-10-02", "views": 4000, "estimatedMinutesWatched": 3000},
            {"video_id": "v2", "date": "2026-09-30", "views": 99999}]

    def test_report_without_revenue_assumption(self):
        report = monthly_report(self.ledgers, self.rows, usd_krw=1400, month="2026-10")
        self.assertEqual((report["episodes"], report["cost_usd"], report["cost_krw"]), (3, "16.0", 22400))
        self.assertEqual((report["views"], report["watch_hours"], report["videos"]), (10000, 150.0, 2))
        self.assertIsNone(report["revenue_usd"])
        self.assertIn("not estimated", report["revenue_note"])
        self.assertTrue(report["trends"]["final"]["improving"])
        self.assertEqual(report["trends"]["script"]["n"], 2)

    def test_report_with_rpm_assumption(self):
        report = monthly_report(self.ledgers, self.rows, 1_000_000, usd_krw=1400, rpm_usd=2.0, month="2026-10")
        self.assertEqual(report["revenue_usd"], 20.0)
        self.assertEqual(report["profit_krw"], 5600)
        self.assertIn("ASSUMPTION", report["revenue_note"])
        self.assertEqual(report["target_progress"], 0.0056)

    def test_report_validation(self):
        with self.assertRaisesRegex(PipelineError, "exchange rate"):
            monthly_report(self.ledgers, self.rows, usd_krw=0)
        with self.assertRaisesRegex(PipelineError, "committed_usd"):
            monthly_report([{"episode": "e1"}], [], usd_krw=1400)


if __name__ == "__main__":
    unittest.main()
