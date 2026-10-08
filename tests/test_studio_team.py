from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from story_pipeline.models import PipelineError
from story_pipeline.studio.config import load_config
from story_pipeline.studio.episode_v3 import validate_episode
from story_pipeline.studio.gateway import AgentGateway, ScriptedTransport, check_schema
from story_pipeline.studio.ledger import BudgetExceeded, CostLedger
from story_pipeline.studio.playbook import Playbook
from story_pipeline.studio.roster import ROSTER, SCHEMAS, check_independence
from story_pipeline.studio.team import Studio

ROOT = Path(__file__).resolve().parents[1]

AGENTS = {spec.name: "gpt-test" for spec in ROSTER}
AGENTS.update({
    "concept-writer-a": "claude-test", "concept-writer-c": "claude-test",
    "greenlight-judge-a": "deepseek-test", "greenlight-judge-b": "deepseek-test",
    "scene-writer": "claude-test", "script-doctor": "claude-test", "final-judge": "grok-test",
})


def write_config(directory: Path, budget: str = "10", script_rounds: int = 3) -> Path:
    lines = [
        "[budget]", f"episode_usd = {budget}",
        "[episode]", "target_characters = [1800, 2600]",
        "[loops]", f"script_rounds = {script_rounds}", "concept_rounds = 2",
        "[channel]", f'directory = "{(ROOT / "channel").as_posix()}"',
        "[agents]", *[f'{name} = "{model}"' for name, model in AGENTS.items()],
        "[prices]", "image_per_call = 0.07",
    ]
    for model in sorted(set(AGENTS.values())):
        lines += [f'[prices.models."{model}"]', "input_per_million = 1", "output_per_million = 2"]
    path = directory / "studio.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def narration(scene: int) -> str:
    body = f"장면{scene} 시작에서 나는 오래된 문을 열었다. " + "그날의 공기는 조용했고 나는 천천히 걸었다. " * 17
    return body + f"전환점{scene}에서 나는 마음을 바꾸었다. " + "우리는 서로를 다시 바라보았다. " * 3


class FakeTeam:
    """Scripted agents that behave like a small, improving production team."""

    def __init__(self, concept_scores=(8.0, 8.0), critic_rounds=None):
        self.concept_scores = list(concept_scores)
        self.critic_rounds = critic_rounds or [{"critic-engagement": 6.0}, {}]
        self.critique_round = 0
        self.critic_calls = 0

    def handlers(self):
        def concept(prompt):
            return {"logline": "엄마와 딸", "hook": "열쇠가 맞지 않는다", "protagonist": "엄마", "conflict": "이사",
                    "turning_point": "딸의 사정", "emotional_payoff": "화해", "title_candidates": ["열쇠"]}

        def judge(prompt):
            score = self.concept_scores[min(len(self.concept_scores) - 1, self.critic_calls // 2)]
            self.critic_calls += 1
            return {"evaluations": [{"concept_index": i, "scores": [{"axis": "hook", "score": score, "rationale": "r"}],
                                     "notes": "n"} for i in range(3)]}

        def outline(prompt):
            return {
                "title": "열쇠", "subtitle": "부제", "logline": "로그라인", "synopsis": "줄거리",
                "characters": [{"id": "mother", "name": "정순", "age_band": "60대", "gender": "female",
                                "appearance": "짧은 회색 머리", "personality": "차분함"},
                               {"id": "daughter", "name": "은정", "age_band": "30대", "gender": "female",
                                "appearance": "긴 머리", "personality": "급함"}],
                "locations": [{"id": "home", "description": "작은 아파트"}],
                "scenes": [{"id": f"{i:02}", "title": f"장면 {i}", "pov": "mother" if i % 2 else "daughter",
                            "location": "home", "mood": "tender", "beats": ["b"], "target_characters": 550}
                           for i in range(1, 5)],
            }

        def scene(prompt):
            identifier = json.loads(prompt.split("```json\n", 1)[1].rsplit("```", 1)[0])["scene"]["id"]
            return {"id": identifier, "narration": narration(int(identifier))}

        def critic(name):
            def handle(prompt):
                round_scores = self.critic_rounds[min(self.critique_round, len(self.critic_rounds) - 1)]
                score = round_scores.get(name, 8.0)
                if name == "critic-policy":
                    self.critique_round += 1
                return {"axis": name, "score": score, "blocking": False,
                        "notes": [{"scene_id": "02", "issue": "늘어짐", "suggestion": "줄여라", "severity": "minor"}]}
            return handle

        def arbiter(prompt):
            return {"decisions": [{"scene_id": "02", "action": "tighten", "rationale": "r", "from_axes": ["engagement"]}],
                    "rejected_notes": []}

        def doctor(prompt):
            return {"scenes": [{"id": "02", "narration": narration(2)}]}

        def director(prompt):
            scene_id = json.loads(prompt.split("```json\n", 1)[1].rsplit("```", 1)[0])["scene"]["id"]
            n = int(scene_id)
            return {"scene_id": scene_id, "shots": [
                {"id": "01", "anchor": f"장면{n} 시작에서", "characters": ["mother"], "visual_prompt": "door",
                 "emotion": "neutral", "reason": "opening", "sfx": ["door"]},
                {"id": "02", "anchor": f"전환점{n}에서", "characters": [], "visual_prompt": "window",
                 "emotion": "softened", "reason": "turn", "sfx": []}]}

        handlers = {
            "trend-researcher": lambda p: {"topics": [{"topic": "이사", "why_now": "봄", "audience_fit": "공감",
                                                       "sources": ["https://example.org/a"]}]},
            "performance-analyst": lambda p: {"keep": [], "avoid": [], "hypotheses": [], "evidence": []},
            "showrunner": lambda p: {"episode_goal": "g", "audience_promise": "p", "constraints": [],
                                     "chosen_topic": "이사", "tone": "따뜻함"},
            "concept-writer-a": concept, "concept-writer-b": concept, "concept-writer-c": concept,
            "greenlight-judge-a": judge, "greenlight-judge-b": judge,
            "outline-writer": outline, "scene-writer": scene, "arbiter": arbiter, "script-doctor": doctor,
            "voice-director": lambda p: {"voices": [
                {"character_id": "mother", "name": "ko-KR-SunHiNeural", "style": "default", "rate": "+0%", "pitch": "+0%"},
                {"character_id": "daughter", "name": "ko-KR-JiMinNeural", "style": "default", "rate": "+0%", "pitch": "+0%"}]},
            "art-director": lambda p: {"style_guide": "watercolor", "negative_guidance": "text", "character_sheets": [
                {"character_id": "mother", "reference_prompt": "m"}, {"character_id": "daughter", "reference_prompt": "d"}]},
            "director": director,
            "packaging-agent": lambda p: {"titles": ["열쇠를 돌려받던 날"], "thumbnail_texts": ["엄마의\n열쇠"],
                                          "description": "d", "tags": ["가족"], "shorts": []},
            "click-judge": lambda p: {"titles": [{"index": 0, "score": 8, "rationale": "r"}],
                                      "thumbnail_texts": [{"index": 0, "score": 8, "rationale": "r"}]},
            "final-judge": lambda p: {"approve": True, "score": 8.2, "reasons": [], "return_to_stage": "none"},
        }
        for critic_name in ("critic-continuity", "critic-engagement", "critic-korean", "critic-originality", "critic-policy"):
            handlers[critic_name] = critic(critic_name)
        return handlers


class StudioTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def studio(self, team: FakeTeam, *, budget="10", script_rounds=3):
        config = load_config(write_config(self.root, budget, script_rounds), environ={})
        run = self.root / "run-001"
        ledger = CostLedger(run / "ledger.json", config.episode_budget_usd)
        transport = ScriptedTransport(team.handlers())
        gateway = AgentGateway(config, ledger, transport, run / "team-trace.jsonl")
        playbook = Playbook(self.root / "playbook.json")
        return Studio(config, gateway, run, playbook, bible="채널 바이블"), transport


class ConfigTests(StudioTestCase):
    def test_repository_config_loads_and_keeps_writers_and_critics_independent(self):
        config = load_config(ROOT / "studio.toml", environ={})
        self.assertEqual(config.episode_budget_usd, Decimal("10"))
        self.assertEqual(set(config.agent_models), {spec.name for spec in ROSTER})
        check_independence(config.agent_models)

    def test_same_family_for_writer_and_critic_is_rejected(self):
        models = dict(AGENTS, **{"critic-korean": "claude-other"})
        with self.assertRaisesRegex(PipelineError, "overlap"):
            check_independence(models)

    def test_unpriced_model_blocks_configuration(self):
        path = write_config(self.root)
        path.write_text(path.read_text(encoding="utf-8").replace('showrunner = "gpt-test"', 'showrunner = "mystery"'),
                        encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "no configured price"):
            load_config(path, environ={})

    def test_every_roster_prompt_and_schema_exists(self):
        for spec in ROSTER:
            self.assertIn(spec.schema, SCHEMAS)
            self.assertTrue(spec.instructions(ROOT / "channel").strip())


class LedgerTests(StudioTestCase):
    def test_reservation_blocks_before_overspend_and_failed_calls_stay_charged(self):
        ledger = CostLedger(self.root / "ledger.json", Decimal("1"))
        first = ledger.reserve("a:x", Decimal("0.6"))
        ledger.fail(first, "timeout")
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("b:y", Decimal("0.5"))
        second = ledger.reserve("b:y", Decimal("0.4"))
        ledger.settle(second, Decimal("0.1"), {})
        reopened = CostLedger(self.root / "ledger.json", Decimal("1"))
        self.assertEqual(reopened.committed(), Decimal("0.7"))

    def test_changed_budget_requires_new_run(self):
        CostLedger(self.root / "ledger.json", Decimal("1")).reserve("a", Decimal("0.1"))
        with self.assertRaisesRegex(PipelineError, "budget differs"):
            CostLedger(self.root / "ledger.json", Decimal("2"))


class SchemaTests(unittest.TestCase):
    def test_schema_checker_rejects_extra_and_wrong_types(self):
        schema = SCHEMAS["critique"]
        good = {"axis": "a", "score": 7, "blocking": False, "notes": []}
        check_schema(good, schema)
        with self.assertRaises(PipelineError):
            check_schema({**good, "extra": 1}, schema)
        with self.assertRaises(PipelineError):
            check_schema({**good, "score": True}, schema)


class TeamTests(StudioTestCase):
    def test_full_preproduction_loops_until_critics_pass(self):
        team = FakeTeam(critic_rounds=[{"critic-engagement": 6.0}, {}])
        studio, transport = self.studio(team)
        result = studio.run_preproduction()
        validate_episode(result["episode"], target_characters=(1800, 2600))
        history = result["script_history"]
        self.assertEqual(len(history), 2)
        self.assertFalse(history[0]["passed"])
        self.assertTrue(history[1]["passed"])
        called = [agent for agent, _ in transport.calls]
        self.assertEqual(called.count("script-doctor"), 1)
        self.assertEqual(called.count("arbiter"), 1)
        self.assertEqual(result["packaging"]["title"], "열쇠를 돌려받던 날")
        self.assertGreater(Decimal(result["budget"]["settled_usd"]), 0)
        trace = (self.root / "run-001" / "team-trace.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(trace), len(transport.calls))

    def test_concept_competition_retries_with_feedback_then_fails(self):
        studio, transport = self.studio(FakeTeam(concept_scores=(5.0, 5.0, 5.0, 5.0)))
        with self.assertRaisesRegex(PipelineError, "Concept gate failed after 2 rounds"):
            studio.concept(studio.plan())
        writer_prompts = [p for a, p in transport.calls if a == "concept-writer-a"]
        self.assertEqual(len(writer_prompts), 2)
        self.assertIn("greenlight-judge-a", writer_prompts[1])

    def test_script_that_never_passes_is_not_produced(self):
        studio, _ = self.studio(FakeTeam(critic_rounds=[{"critic-engagement": 5.0}]), script_rounds=3)
        with self.assertRaisesRegex(PipelineError, "Script gate failed"):
            studio.run_preproduction()

    def test_completed_stages_resume_without_new_calls(self):
        studio, transport = self.studio(FakeTeam())
        studio.run_preproduction()
        count = len(transport.calls)
        again, transport2 = self.studio(FakeTeam())
        again.run_preproduction()
        self.assertEqual(len(transport2.calls), 0)
        self.assertGreater(count, 20)

    def test_budget_stops_the_team(self):
        studio, _ = self.studio(FakeTeam(), budget="0.05")
        with self.assertRaises(BudgetExceeded):
            studio.run_preproduction()

    def test_unsourced_research_is_refused(self):
        team = FakeTeam()
        studio, transport = self.studio(team)
        transport.handlers["trend-researcher"] = lambda p: {"topics": [
            {"topic": "x", "why_now": "y", "audience_fit": "z", "sources": []}]}
        with self.assertRaisesRegex(PipelineError, "no sourced topics"):
            studio.plan()

    def test_final_gate_requires_technical_pass_and_threshold(self):
        studio, _ = self.studio(FakeTeam())
        pre = studio.run_preproduction()
        self.assertTrue(studio.final_review(pre, {"technical_pass": True})["approved"])
        self.assertFalse(studio.final_review(pre, {"technical_pass": False}, attempt=2)["approved"])


class PlaybookTests(StudioTestCase):
    def test_lessons_are_role_scoped_deduplicated_and_retired_not_deleted(self):
        playbook = Playbook(self.root / "p.json")
        first = playbook.add("짧게 써라", ["scene-writer"], "low", "ep1", "ep1")
        self.assertEqual(playbook.add("짧게 써라", ["scene-writer"], "low", "ep2", "ep2"), first)
        playbook.add("후크를 앞에", ["concept-writer"], "medium", "ep1", "ep1")
        self.assertEqual(len(playbook.active("concept-writer-b")), 1)
        self.assertEqual(len(playbook.active("art-critic")), 0)
        playbook.retire(first, "성과 반박", "ep3")
        self.assertEqual(playbook.save(), 1)
        reopened = Playbook(self.root / "p.json")
        self.assertEqual(len(reopened.lessons), 2)
        self.assertNotIn("짧게 써라", reopened.markdown())

    def test_repository_seed_playbook_is_valid(self):
        playbook = Playbook(ROOT / "channel" / "playbook.json")
        self.assertGreaterEqual(len(playbook.active("director")), 3)


if __name__ == "__main__":
    unittest.main()
