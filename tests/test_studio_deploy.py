"""Deployment helpers for the Azure studio: agents, knowledge, model checks, hosting, telemetry.

Azure clients are replaced by in-memory fakes; nothing here touches the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from story_pipeline.models import PipelineError
from story_pipeline.studio import telemetry
from story_pipeline.studio.check_models import check_models
from story_pipeline.studio.config import load_config
from story_pipeline.studio.deploy_agents import build_tools, deploy_agents, sync_knowledge
from story_pipeline.studio.hosted import RunStore, RunTracker, StudioRequest, parse_request
from story_pipeline.studio.playbook import Playbook
from story_pipeline.studio.roster import ROSTER

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _has(module: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    return True


class FakeModels:
    """Stand-in for azure.ai.projects.models with the same constructor names."""

    class WebSearchTool(SimpleNamespace):
        type = "web_search"

    class FileSearchTool(SimpleNamespace):
        type = "file_search"

    class PromptAgentDefinition(SimpleNamespace):
        pass

    class PromptAgentDefinitionTextOptions(SimpleNamespace):
        pass

    class TextResponseFormatJsonSchema(SimpleNamespace):
        pass


class ResourceNotFoundError(Exception):
    status_code = 404


class FakeVectorFiles:
    def __init__(self, owner):
        self.owner = owner

    def upload_and_poll(self, *, vector_store_id, file, attributes=None):
        name, content = file
        self.owner.counter += 1
        file_id = f"file-{self.owner.counter}"
        self.owner.store_files[vector_store_id][file_id] = {"name": name, "content": content,
                                                           "attributes": attributes}
        self.owner.uploads.append(name)
        return SimpleNamespace(id=file_id, status="completed", last_error=None)

    def delete(self, file_id, *, vector_store_id):
        self.owner.store_files[vector_store_id].pop(file_id, None)
        self.owner.deleted.append(file_id)

    def list(self, *, vector_store_id):
        return [SimpleNamespace(id=file_id, attributes=item["attributes"])
                for file_id, item in self.owner.store_files[vector_store_id].items()]


class FakeVectorStores:
    def __init__(self, owner):
        self.owner = owner
        self.files = FakeVectorFiles(owner)

    def create(self, *, name):
        store_id = f"vs_{len(self.owner.stores) + 1}"
        self.owner.stores[store_id] = name
        self.owner.store_files[store_id] = {}
        return SimpleNamespace(id=store_id, name=name)

    def retrieve(self, store_id):
        if store_id not in self.owner.stores:
            raise ResourceNotFoundError(store_id)
        return SimpleNamespace(id=store_id, name=self.owner.stores[store_id])

    def list(self):
        return [SimpleNamespace(id=key, name=value) for key, value in self.owner.stores.items()]


class FakeOpenAI:
    def __init__(self):
        self.stores: dict[str, str] = {}
        self.store_files: dict[str, dict[str, dict]] = {}
        self.uploads: list[str] = []
        self.deleted: list[str] = []
        self.counter = 0
        self.vector_stores = FakeVectorStores(self)
        self.files = SimpleNamespace(delete=lambda file_id: self.deleted.append(f"files:{file_id}"))


class FakeAgents:
    def __init__(self):
        self.created: list[dict] = []
        self.latest: dict[str, SimpleNamespace] = {}

    def create_version(self, *, agent_name, definition, description=None, metadata=None):
        version = str(sum(1 for call in self.created if call["agent_name"] == agent_name) + 1)
        self.created.append({"agent_name": agent_name, "definition": definition, "metadata": metadata})
        self.latest[agent_name] = SimpleNamespace(version=version, metadata=metadata)
        return SimpleNamespace(name=agent_name, version=version)

    def get(self, *, agent_name):
        if agent_name not in self.latest:
            raise ResourceNotFoundError(agent_name)
        return SimpleNamespace(name=agent_name, versions=SimpleNamespace(latest=self.latest[agent_name]))


class DeployAgentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.channel = self.temp / "channel"
        shutil.copytree(ROOT / "channel", self.channel)
        self.config = load_config(environ={})
        self.playbook = Playbook(self.temp / "playbook.json")
        self.project = SimpleNamespace(agents=FakeAgents())
        self.openai = FakeOpenAI()
        self.manifest = self.temp / "agents-manifest.json"
        self.state = self.temp / "knowledge-state.json"

    def deploy(self, **overrides):
        options = dict(channel_dir=self.channel, playbook=self.playbook, manifest_path=self.manifest,
                       state_path=self.state, models=FakeModels)
        options.update(overrides)
        return deploy_agents(self.project, self.openai, self.config, **options)

    def test_first_deploy_creates_every_agent_with_mapped_tools_and_manifest(self):
        manifest = self.deploy()
        created = {call["agent_name"]: call for call in self.project.agents.created}
        self.assertEqual(set(created), {spec.name for spec in ROSTER})
        store_id = manifest["vector_store_id"]
        self.assertEqual(sorted(self.openai.uploads), ["bible.md", "playbook.md", "rubrics.md"])

        showrunner = created["showrunner"]["definition"]
        self.assertEqual(showrunner.model, self.config.model_for("showrunner"))
        self.assertEqual([tool.type for tool in showrunner.tools], ["file_search"])
        self.assertEqual(showrunner.tools[0].vector_store_ids, [store_id])
        self.assertEqual([tool.type for tool in created["trend-researcher"]["definition"].tools], ["web_search"])
        self.assertEqual(created["greenlight-judge-a"]["definition"].tools, [])
        self.assertIn((self.channel / "prompts" / "_shared.md").read_text(encoding="utf-8"),
                      showrunner.instructions)

        saved = json.loads(self.manifest.read_text(encoding="utf-8"))
        entry = saved["agents"]["critic-originality"]
        self.assertEqual(entry["version"], "1")
        self.assertEqual(entry["model"], self.config.model_for("critic-originality"))
        self.assertEqual(len(entry["instructions_sha256"]), 64)
        self.assertEqual(entry["tools"], [])
        self.assertTrue(all(item["changed"] for item in saved["agents"].values()))

    def test_second_deploy_is_a_no_op(self):
        self.deploy()
        created, uploads = len(self.project.agents.created), len(self.openai.uploads)
        manifest = self.deploy()
        self.assertEqual(len(self.project.agents.created), created)
        self.assertEqual(len(self.openai.uploads), uploads)
        self.assertFalse(any(item["changed"] for item in manifest["agents"].values()))

    def test_only_changed_prompt_gets_a_new_version(self):
        self.deploy()
        before = len(self.project.agents.created)
        prompt = self.channel / "prompts" / "arbiter.md"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\n추가 규칙.\n", encoding="utf-8")
        manifest = self.deploy()
        new = [call["agent_name"] for call in self.project.agents.created[before:]]
        self.assertEqual(new, ["arbiter"])
        self.assertEqual(manifest["agents"]["arbiter"]["version"], "2")

    def test_model_change_creates_new_version(self):
        self.deploy()
        before = len(self.project.agents.created)
        models = dict(self.config.agent_models, retrospective="claude-sonnet-5")
        self.config = type(self.config)(**{**vars(self.config), "agent_models": models})
        self.deploy()
        self.assertEqual([c["agent_name"] for c in self.project.agents.created[before:]], ["retrospective"])

    def test_changed_knowledge_file_is_replaced_without_new_agent_versions(self):
        self.deploy()
        created = len(self.project.agents.created)
        (self.channel / "bible.md").write_text("# 새 바이블\n", encoding="utf-8")
        self.deploy()
        self.assertEqual(self.openai.uploads.count("bible.md"), 2)
        self.assertEqual(self.openai.uploads.count("rubrics.md"), 1)
        self.assertTrue(any(item.startswith("file-") for item in self.openai.deleted))
        self.assertEqual(len(self.project.agents.created), created)

    def test_fresh_checkout_reuses_store_and_remote_versions(self):
        self.deploy()
        created, uploads = len(self.project.agents.created), len(self.openai.uploads)
        self.manifest.unlink()
        self.state.unlink()
        manifest = self.deploy()
        self.assertEqual(len(self.project.agents.created), created)
        self.assertEqual(len(self.openai.uploads), uploads)
        self.assertEqual(manifest["agents"]["showrunner"]["version"], "1")
        self.assertEqual(len(self.openai.stores), 1)

    def test_sync_knowledge_requires_bible(self):
        (self.channel / "bible.md").unlink()
        with self.assertRaisesRegex(PipelineError, "bible"):
            sync_knowledge(self.openai, self.channel, self.playbook, state_path=self.state)

    def test_unknown_tool_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "Unsupported"):
            build_tools(["code_interpreter"], "vs_1", FakeModels)
        with self.assertRaisesRegex(PipelineError, "vector store"):
            build_tools(["file_search"], None, FakeModels)

    @unittest.skipUnless(_has("azure.ai.projects"), "azure-ai-projects not installed")
    def test_real_tool_models(self):
        from azure.ai.projects.models import FileSearchTool, WebSearchTool

        tools = build_tools(["web_search", "file_search"], "vs_9")
        self.assertIsInstance(tools[0], WebSearchTool)
        self.assertIsInstance(tools[1], FileSearchTool)
        self.assertEqual(tools[1].vector_store_ids, ["vs_9"])


class CheckModelsTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config(environ={})

    def project(self, names):
        items = [SimpleNamespace(name=name, type="ModelDeployment", model_name=name, model_version="1",
                                 model_publisher="x") for name in names]
        return SimpleNamespace(deployments=SimpleNamespace(list=lambda: items))

    def test_all_present(self):
        names = set(self.config.agent_models.values()) | {self.config.image_deployment}
        report = check_models(self.config, self.project(names))
        self.assertEqual(report["missing"], {})
        self.assertEqual(report["independence"], "ok")
        self.assertIn("image", report["required"][self.config.image_deployment])

    def test_missing_deployment_raises(self):
        names = set(self.config.agent_models.values()) - {"grok-4-1"}
        with self.assertRaisesRegex(PipelineError, "grok-4-1"):
            check_models(self.config, self.project(names))

    def test_missing_image_deployment_raises(self):
        with self.assertRaisesRegex(PipelineError, self.config.image_deployment):
            check_models(self.config, self.project(set(self.config.agent_models.values())))


class HostedTest(unittest.TestCase):
    def test_core_imports_without_agent_framework(self):
        code = (
            "import sys\n"
            "for name in ('agent_framework', 'agent_framework_foundry_hosting', 'azure.ai.agentserver',"
            " 'azure.ai.projects', 'azure.monitor.opentelemetry'):\n"
            "    sys.modules[name] = None\n"
            "import story_pipeline.studio.hosted as h, story_pipeline.studio.deploy_agents,"
            " story_pipeline.studio.check_models, story_pipeline.studio.telemetry as t\n"
            "assert h.parse_request('{\"run_id\": \"r1\"}').run_id == 'r1'\n"
            "assert t.configure(environ={}) is False\n"
            "print('ok')\n"
        )
        env = dict(os.environ, PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
        env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_parse_request_shapes(self):
        self.assertEqual(parse_request({"run_id": "a-1"}).run_id, "a-1")
        self.assertEqual(parse_request(['{"run_id": "b.2", "retry": true}']).retry, True)
        self.assertEqual(parse_request({"message": '{"run_id": "c"}'}).run_id, "c")
        self.assertEqual(parse_request(SimpleNamespace(text='{"run_id": "d"}')).run_id, "d")
        for bad in ("not json", {"run_id": "../escape"}, {"run_id": ""}, [], {"run_id": "x", "history": [1]}):
            with self.assertRaises(PipelineError):
                parse_request(bad)

    def test_run_store_disabled_without_account(self):
        store = RunStore(environ={})
        self.assertFalse(store.enabled)
        self.assertEqual(store.upload("r", Path("missing")), 0)
        self.assertEqual(store.download("r", Path("missing")), 0)

    @unittest.skipUnless(_has("agent_framework"), "agent-framework not installed")
    def test_workflow_agent_runs_and_tracker_is_idempotent(self):
        from story_pipeline.studio.hosted import build_agent

        temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        environ = {"STUDIO_RUNS_DIR": str(temp)}
        calls = []

        def runner(request):
            calls.append(request.run_id)
            if request.run_id == "boom":
                raise PipelineError("gate failed")
            return {"run_id": request.run_id, "status": "completed", "title": "제목"}

        def factory(run_id):
            return build_agent(run_id, runner=runner, store=RunStore(environ), environ=environ)

        async def scenario():
            response = await factory("direct").run([json.dumps({"run_id": "direct"})])
            self.assertEqual(json.loads(response.text)["title"], "제목")
            self.assertTrue((temp / "direct" / "workflow-checkpoints").is_dir())

            tracker = RunTracker(factory, environ)
            self.assertEqual((await tracker.submit(StudioRequest("ep1")))[0], 202)
            self.assertEqual((await tracker.submit(StudioRequest("ep1")))[1]["status"], "running")
            await tracker.tasks["ep1"]
            code, payload = await tracker.submit(StudioRequest("ep1"))
            self.assertEqual((code, payload["status"]), (200, "completed"))
            self.assertEqual(tracker.lookup("ep1")[1]["status"], "completed")

            await tracker.submit(StudioRequest("boom"))
            await tracker.tasks["boom"]
            code, payload = await tracker.submit(StudioRequest("boom"))
            self.assertEqual(payload["status"], "failed")
            self.assertIn("gate failed", payload["error"])
            self.assertEqual((await tracker.submit(StudioRequest("boom", retry=True)))[0], 202)
            await tracker.tasks["boom"]

        with _quiet():
            asyncio.run(scenario())
        self.assertEqual(calls.count("ep1"), 1)
        self.assertEqual(calls.count("boom"), 2)
        self.assertEqual(RunTracker(environ=environ).lookup("unknown")[0], 404)


class _quiet:
    def __enter__(self):
        import warnings

        self._catcher = warnings.catch_warnings()
        self._catcher.__enter__()
        warnings.simplefilter("ignore")

    def __exit__(self, *exc):
        return self._catcher.__exit__(*exc)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        telemetry._configured = False
        self.addCleanup(setattr, telemetry, "_configured", False)

    def test_no_connection_string_is_a_no_op(self):
        with mock.patch.dict(sys.modules, {"azure.monitor.opentelemetry": None}):
            self.assertFalse(telemetry.configure(environ={}))
            self.assertFalse(telemetry.configure(environ={telemetry.CONNECTION_VARIABLE: "  "}))

    def test_connection_string_without_package_fails_loudly(self):
        with mock.patch.dict(sys.modules, {"azure.monitor.opentelemetry": None}):
            with self.assertRaisesRegex(PipelineError, "azure-monitor-opentelemetry"):
                telemetry.configure(environ={telemetry.CONNECTION_VARIABLE: "InstrumentationKey=x"})

    def test_connection_string_configures_azure_monitor_once(self):
        calls = []
        fake = SimpleNamespace(configure_azure_monitor=lambda **kwargs: calls.append(kwargs))
        environ = {telemetry.CONNECTION_VARIABLE: "InstrumentationKey=00000000-0000-0000-0000-000000000000"}
        with mock.patch.dict(sys.modules, {"azure.monitor.opentelemetry": fake,
                                           "agent_framework.observability": None}), \
                mock.patch.dict(os.environ):
            self.assertTrue(telemetry.configure(environ=environ))
            self.assertTrue(telemetry.configure(environ=environ))
        self.assertEqual(calls, [{"connection_string": environ[telemetry.CONNECTION_VARIABLE]}])


if __name__ == "__main__":
    unittest.main()
