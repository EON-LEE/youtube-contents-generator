from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from story_pipeline.models import PipelineError
from story_pipeline.studio import cli as studio_cli
from story_pipeline.studio.analytics import JsonAnalyticsStore
from story_pipeline.studio.gateway import ScriptedTransport
from story_pipeline.studio.pipeline import Production
from test_studio_pipeline import FakeMedia, team_with_extras
from test_studio_team import ROOT, StudioTestCase, write_config


class FakeAnalytics:
    def reports(self):
        return self

    def query(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        names = ["day", *self.kwargs["metrics"].split(",")]
        return {"columnHeaders": [{"name": n} for n in names],
                "rows": [["2026-10-03", *[10 if n != "averageViewPercentage" else 42.0 for n in names[1:]]]]}


class FakeLeaseError(Exception):
    status_code = 409


class FakeBlobContainer:
    """Minimal ContainerClient stand-in with one lockable blob."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.leased = False
        self.metadata: dict[str, str] = {}
        self.broken = 0

    def list_blobs(self):
        return [__import__("types").SimpleNamespace(name=n) for n in self.blobs]

    def download_blob(self, name):
        return mock.Mock(readall=lambda: self.blobs[name])

    def upload_blob(self, name, data, overwrite=True):
        from azure.core.exceptions import ResourceExistsError
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("exists")
        self.blobs[name] = data if isinstance(data, bytes) else data.read()

    def get_blob_client(self, name):
        container = self

        class Blob:
            def upload_blob(self, data, overwrite=True):
                container.upload_blob(name, data, overwrite)

            def acquire_lease(self, lease_duration=-1):
                from azure.core.exceptions import HttpResponseError
                if container.leased:
                    error = HttpResponseError("leased")
                    error.status_code = 409
                    raise error
                container.leased = True
                return mock.Mock(release=lambda: setattr(container, "leased", False))

            def set_blob_metadata(self, metadata, lease=None):
                container.metadata = metadata

            def get_blob_properties(self):
                return mock.Mock(metadata=container.metadata)

            def break_lease(self, lease_break_period=0):
                container.leased = False
                container.broken += 1
        return Blob()


def _has_azure_core() -> bool:
    try:
        import azure.core  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(_has_azure_core(), "azure-core not installed")
class StateTransactionTests(StudioTestCase):
    def store(self, container, now):
        from story_pipeline.studio.state import StateStore
        clock = {"t": now}
        return StateStore(self.root / "st", environ={}, container_client=container,
                          clock=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)), clock

    def test_transaction_repulls_and_pushes_under_lease(self):
        container = FakeBlobContainer()
        container.blobs["videos.json"] = b'[{"video_id": "a"}]'
        store, _ = self.store(container, 1000)
        with store.transaction():
            self.assertTrue(container.leased)
            videos = json.loads(store.path("videos.json").read_text(encoding="utf-8"))
            store.path("videos.json").write_text(json.dumps(videos + [{"video_id": "b"}]), encoding="utf-8")
        self.assertFalse(container.leased)
        self.assertEqual(len(json.loads(container.blobs["videos.json"])), 2)

    def test_busy_lock_times_out_and_stale_lock_is_broken(self):
        container = FakeBlobContainer()
        container.blobs["_lock"] = b""
        container.leased = True
        container.metadata = {"acquired_at": "1000"}
        store, _ = self.store(container, 1100)
        with self.assertRaisesRegex(PipelineError, "locked"):
            with store.transaction(wait_seconds=60):
                pass
        stale, _ = self.store(container, 1000 + 3 * 3600)
        with stale.transaction():
            pass
        self.assertEqual(container.broken, 1)


class StudioCliTests(StudioTestCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {
            "STUDIO_CONFIG": str(write_config(self.root)), "STUDIO_STATE_DIR": str(self.root / "state"),
            "STUDIO_RUNS_DIR": str(self.root / "runs"), "STUDIO_CHANNEL_DIR": str(ROOT / "channel"),
            "STUDIO_STORAGE_ACCOUNT_URL": "", "FOUNDRY_PROJECT_ENDPOINT": "",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def produced_run(self) -> str:
        studio, _ = self.studio(team_with_extras())
        Production(studio, FakeMedia().ops(), self.root / "m", self.root / "s").produce()
        target = self.root / "runs" / "run-001"
        target.parent.mkdir(parents=True, exist_ok=True)
        studio.run_dir.rename(target)
        return "run-001"

    def test_state_seeds_playbook_from_repository(self):
        _, _, state, playbook = studio_cli._context()
        self.assertTrue(state.path("playbook.json").exists())
        self.assertGreaterEqual(len(playbook.active("director")), 3)

    def test_collect_then_learn_runs_post_analytics_retrospective_once(self):
        run = self.produced_run()
        state = self.root / "state"
        state.mkdir(exist_ok=True)
        (state / "videos.json").write_text(json.dumps([{
            "video_id": "vid1", "run": run, "title": "열쇠", "uploaded_at": "2026-09-30T00:00:00+00:00",
            "shorts": [], "post_analytics_retro": False}]), encoding="utf-8")
        collected = studio_cli.command_collect_analytics(dt.date(2026, 10, 8), services=lambda: (None, FakeAnalytics()))
        self.assertEqual(collected["total_rows"], 1)
        transport = ScriptedTransport(team_with_extras().handlers())
        report = studio_cli.command_learn(dt.date(2026, 10, 8), transport=transport)
        self.assertEqual(len(report["retrospectives"]), 1)
        self.assertEqual([agent for agent, _ in transport.calls], ["retrospective"])
        self.assertTrue(json.loads((state / "videos.json").read_text(encoding="utf-8"))[0]["post_analytics_retro"])
        self.assertTrue((state / "calibration.json").exists())
        again = studio_cli.command_learn(dt.date(2026, 10, 9), transport=ScriptedTransport(team_with_extras().handlers()))
        self.assertEqual(again["retrospectives"], [])
        rows = JsonAnalyticsStore(state / "analytics.json").rows()
        self.assertEqual(rows[0]["averageViewPercentage"], 42.0)

    def test_uploader_maps_shorts_uses_persistent_quota_and_records_video_immediately(self):
        _, _, state, _ = studio_cli._context()
        captured = {}

        def fake_upload(service, video, metadata, thumbnail, captions, shorts, quota, **kw):
            captured.update(shorts=shorts)
            return {"status": "uploaded", "video_id": "x", "shorts": ["s1"]}

        with mock.patch("story_pipeline.studio.youtube.upload_episode", fake_upload):
            upload = studio_cli.make_uploader(state, "run-9", services=lambda: ("yt", "an"))
            result = upload({"video": "v.mp4", "metadata": {"title": "열쇠"}, "thumbnail": "t.jpg", "captions": ["c.srt"],
                             "shorts": [{"file": "s.mp4", "hook": "엄마의 비밀"}]})
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(captured["shorts"], [{"path": "s.mp4", "title": "엄마의 비밀 | 열쇠", "description": "엄마의 비밀"}])
        videos = json.loads(state.path("videos.json").read_text(encoding="utf-8"))
        self.assertEqual((videos[0]["video_id"], videos[0]["run"]), ("x", "run-9"))

    def test_job_rejects_missing_library_before_any_agent_call(self):
        transport = ScriptedTransport({})
        with self.assertRaisesRegex(Exception, "library"):
            studio_cli.command_job("run-x", transport=transport)
        self.assertEqual(transport.calls, [])

    def test_job_keeps_upload_record_when_retrospective_fails(self):
        library = self.root / "library"
        for kind in ("music", "sfx"):
            (library / kind).mkdir(parents=True)
            (library / kind / "library.json").write_text("[]", encoding="utf-8")
        handlers = team_with_extras().handlers()
        handlers["retrospective"] = lambda p: (_ for _ in ()).throw(PipelineError("budget"))
        with mock.patch.dict(os.environ, {"STUDIO_LIBRARY_DIR": str(library), "STUDIO_UPLOAD": "private"}), \
                mock.patch("story_pipeline.studio.audio_mix.load_library", return_value=[]), \
                mock.patch.object(studio_cli, "make_uploader",
                                  lambda state, run_id: lambda package: {"status": "uploaded", "video_id": "v9"}):
            result = studio_cli.command_job("run-7", media=FakeMedia().ops(), transport=ScriptedTransport(handlers))
        self.assertEqual(result["upload"]["video_id"], "v9")
        self.assertIn("error", result["retrospective"])

    def test_top_level_cli_routes_studio_commands(self):
        from story_pipeline.cli import main
        with mock.patch.object(studio_cli, "command_collect_analytics", return_value={"collected": 0}):
            self.assertEqual(main(["studio", "collect-analytics"]), 0)
        self.assertEqual(main(["studio", "job", "--run-id", ""]), 1)


if __name__ == "__main__":
    unittest.main()
