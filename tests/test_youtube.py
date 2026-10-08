from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from story_pipeline.models import PipelineError
from story_pipeline.studio.youtube import (
    SCOPES, SYNTHETIC_MEDIA_FIELD, EnvSecretProvider, KeyVaultSecretProvider, QuotaLedger, Retry, SecretNames,
    YouTubeSettings, audit_packet, pacific_day, plan_cost, tags_length, upload_episode, validate_metadata,
    youtube_credentials,
)


class HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class FakeRequest:
    def __init__(self, service, method, kwargs):
        self.service, self.method, self.kwargs = service, method, kwargs
        self.chunks = 0

    def _fail(self):
        queue = self.service.failures.get(self.method, [])
        if queue:
            raise queue.pop(0)

    def next_chunk(self):
        self._fail()
        self.chunks += 1
        if self.chunks == 1:
            return SimpleNamespace(progress=lambda: 0.5), None
        self.service.counter += 1
        privacy = self.service.privacy_override or self.kwargs["body"]["status"]["privacyStatus"]
        return None, {"id": f"vid{self.service.counter}", "status": {"privacyStatus": privacy}}

    def execute(self):
        self._fail()
        self.service.counter += 1
        return {"id": f"{self.method}-{self.service.counter}"}


class FakeResource:
    def __init__(self, service, name):
        self.service, self.name = service, name

    def __getattr__(self, verb):
        def call(**kwargs):
            self.service.calls.append((f"{self.name}.{verb}", kwargs))
            return FakeRequest(self.service, f"{self.name}.{verb}", kwargs)
        return call


class FakeYouTube:
    def __init__(self, failures=None, privacy_override=None):
        self.calls: list[tuple[str, dict]] = []
        self.failures = failures or {}
        self.counter = 0
        self.privacy_override = privacy_override

    def videos(self):
        return FakeResource(self, "videos")

    def thumbnails(self):
        return FakeResource(self, "thumbnails")

    def captions(self):
        return FakeResource(self, "captions")


def media(path, mimetype, resumable):
    return {"path": path, "mimetype": mimetype, "resumable": resumable}


NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)


class YouTubeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        for name in ("ep.mp4", "s1.mp4", "s2.mp4", "thumb.png", "ep.srt"):
            (self.root / name).write_bytes(b"data")
        self.sleeps: list[float] = []
        self.retry = Retry(max_retries=3, sleep=self.sleeps.append, rng=lambda: 1.0)
        self.metadata = {"title": "바뀐 자물쇠", "description": "가족 오디오 드라마", "tags": ["오디오드라마", "가족"]}

    def tearDown(self):
        self.dir.cleanup()

    def ledger(self, **kwargs):
        return QuotaLedger(self.root / "quota.json", clock=kwargs.pop("clock", lambda: NOW), **kwargs)

    def upload(self, service, quota=None, shorts=None, **kwargs):
        shorts = [{"path": self.root / "s1.mp4", "title": "열쇠의 비밀"}] if shorts is None else shorts
        return upload_episode(service, self.root / "ep.mp4", self.metadata, self.root / "thumb.png",
                              self.root / "ep.srt", shorts, quota or self.ledger(), media_factory=media,
                              retry=self.retry, clock=lambda: NOW, **kwargs)

    def test_env_credentials_use_all_scopes(self):
        recorded = {}
        provider = EnvSecretProvider({"YOUTUBE_CLIENT_ID": "id", "YOUTUBE_CLIENT_SECRET": "secret",
                                      "YOUTUBE_REFRESH_TOKEN": "refresh"})
        youtube_credentials(provider, credentials_cls=lambda **kw: recorded.update(kw))
        self.assertEqual(recorded["refresh_token"], "refresh")
        self.assertEqual(recorded["client_id"], "id")
        self.assertEqual(recorded["scopes"], list(SCOPES))
        self.assertIn("https://www.googleapis.com/auth/youtube.force-ssl", SCOPES)
        with self.assertRaisesRegex(PipelineError, "YOUTUBE_REFRESH_TOKEN"):
            youtube_credentials(EnvSecretProvider({}), credentials_cls=dict)

    def test_key_vault_provider_reads_configured_names(self):
        values = {"yt-id": "a", "yt-secret": "b", "yt-refresh": "c", "empty": ""}
        client = SimpleNamespace(get_secret=lambda name: SimpleNamespace(value=values[name]))
        provider = KeyVaultSecretProvider("", client=client)
        names = SecretNames("yt-id", "yt-secret", "yt-refresh")
        recorded = {}
        youtube_credentials(provider, names, credentials_cls=lambda **kw: recorded.update(kw))
        self.assertEqual((recorded["client_id"], recorded["client_secret"], recorded["refresh_token"]), ("a", "b", "c"))
        with self.assertRaisesRegex(PipelineError, "empty"):
            provider.get("empty")
        with self.assertRaisesRegex(PipelineError, "Cannot read"):
            provider.get("missing")

    def test_pacific_day_follows_dst(self):
        cases = {
            datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc): "2026-06-30",
            datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc): "2026-07-01",
            datetime(2026, 1, 1, 7, 59, tzinfo=timezone.utc): "2025-12-31",
            datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc): "2026-01-01",
            datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc): "2026-03-07",
            datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc): "2026-11-01",
            datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc): "2026-11-01",
        }
        for moment, expected in cases.items():
            self.assertEqual(pacific_day(moment), expected, moment)
        with self.assertRaises(PipelineError):
            pacific_day(datetime(2026, 1, 1))

    def test_upload_is_private_with_disclosures_and_quota(self):
        service = FakeYouTube()
        quota = self.ledger()
        record = self.upload(service, quota)
        self.assertEqual(record["status"], "uploaded")
        self.assertEqual(record["privacy"], "private")
        self.assertEqual(record["video_id"], "vid1")
        self.assertEqual(len(record["shorts"]), 1)
        self.assertEqual(record["quota_used"], {"units": 450, "uploads": 2})
        inserts = [kw for method, kw in service.calls if method == "videos.insert"]
        for kwargs in inserts:
            self.assertEqual(kwargs["part"], "snippet,status")
            status = kwargs["body"]["status"]
            self.assertEqual(status, {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                                      SYNTHETIC_MEDIA_FIELD: True})
            snippet = kwargs["body"]["snippet"]
            self.assertEqual((snippet["categoryId"], snippet["defaultLanguage"], snippet["defaultAudioLanguage"]),
                             ("24", "ko", "ko"))
            self.assertTrue(kwargs["media_body"]["resumable"])
        short = inserts[1]["body"]["snippet"]
        self.assertIn("#Shorts", short["title"])
        self.assertIn("#Shorts", short["description"])
        self.assertIn("https://www.youtube.com/watch?v=vid1", short["description"])
        caption = next(kw for m, kw in service.calls if m == "captions.insert")
        self.assertEqual(caption["body"]["snippet"], {"videoId": "vid1", "language": "ko", "name": "한국어",
                                                      "isDraft": False})
        thumb = next(kw for m, kw in service.calls if m == "thumbnails.set")
        self.assertEqual((thumb["videoId"], thumb["media_body"]["mimetype"]), ("vid1", "image/png"))
        self.assertEqual(quota.used(), {"units": 450, "uploads": 2})
        reloaded = QuotaLedger(self.root / "quota.json", clock=lambda: NOW)
        self.assertEqual(reloaded.used(), {"units": 450, "uploads": 2})

    def test_retries_server_errors_with_backoff(self):
        service = FakeYouTube({"videos.insert": [HttpError(503), ConnectionError("reset")],
                               "captions.insert": [HttpError(500)]})
        record = self.upload(service, shorts=[])
        self.assertEqual(record["status"], "uploaded")
        self.assertEqual(self.sleeps, [2.0, 4.0, 2.0])

    def test_non_retryable_and_exhausted_errors_raise(self):
        with self.assertRaisesRegex(PipelineError, "failed: HTTP 403"):
            self.upload(FakeYouTube({"videos.insert": [HttpError(403)]}))
        self.assertEqual(self.sleeps, [])
        with self.assertRaisesRegex(PipelineError, "after 3 retries"):
            self.upload(FakeYouTube({"videos.insert": [HttpError(503)] * 4}))

    def test_failure_after_main_upload_names_the_video(self):
        with self.assertRaisesRegex(PipelineError, "main video vid1 is already uploaded as private"):
            self.upload(FakeYouTube({"thumbnails.set": [HttpError(400)]}))

    def test_non_private_response_is_an_error(self):
        with self.assertRaisesRegex(PipelineError, "not private"):
            self.upload(FakeYouTube(privacy_override="public"))

    def test_insufficient_quota_defers_without_calls(self):
        service = FakeYouTube()
        record = self.upload(service, self.ledger(daily_units=400))
        self.assertEqual(record["status"], "deferred")
        self.assertEqual(record["quota_needed"], {"units": 450, "uploads": 2})
        self.assertEqual(service.calls, [])
        record = self.upload(service, self.ledger(daily_uploads=1))
        self.assertEqual(record["status"], "deferred")

    def test_quota_resets_on_next_pacific_day(self):
        moment = [NOW]
        quota = self.ledger(daily_units=500, clock=lambda: moment[0])
        self.assertEqual(self.upload(FakeYouTube(), quota)["status"], "uploaded")
        self.assertEqual(self.upload(FakeYouTube(), quota)["status"], "deferred")
        moment[0] = NOW + timedelta(days=1)
        self.assertEqual(quota.used(), {"units": 0, "uploads": 0})
        self.assertEqual(self.upload(FakeYouTube(), quota)["status"], "uploaded")
        with self.assertRaisesRegex(PipelineError, "exceed today's quota"):
            quota.charge("captions.insert")

    def test_metadata_limits(self):
        validate_metadata(self.metadata)
        bad = [
            ({"title": "가" * 101}, "limit is 100"),
            ({"title": "a <b>"}, "'<' or '>'"),
            ({"title": "ok", "description": "가" * 1700}, "5000 UTF-8 bytes"),
            ({"title": "ok", "tags": ["x" * 250, "y" * 250]}, "limit is 500"),
            ({"title": "ok", "privacyStatus": "public"}, "always private"),
            ({"title": "ok", "tags": ["fine", "a>b"]}, "'<' or '>'"),
            ({"title": " "}, "title is required"),
        ]
        for metadata, message in bad:
            with self.assertRaisesRegex(PipelineError, message):
                validate_metadata(metadata)
        self.assertEqual(tags_length(["a b", "cd"]), 3 + 2 + 2 + 1)

    def test_invalid_inputs_fail_before_any_call(self):
        service = FakeYouTube()
        with self.assertRaisesRegex(PipelineError, "Short 1"):
            self.upload(service, shorts=[{"path": self.root / "missing.mp4", "title": "x"}])
        with self.assertRaisesRegex(PipelineError, "short 1: title has"):
            self.upload(service, shorts=[{"path": self.root / "s1.mp4", "title": "가" * 95}])
        (self.root / "big.png").write_bytes(b"0" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(PipelineError, "2 MB"):
            upload_episode(service, self.root / "ep.mp4", self.metadata, self.root / "big.png", None, [],
                           self.ledger(), media_factory=media, retry=self.retry)
        self.assertEqual(service.calls, [])

    def test_plan_cost_and_audit_packet(self):
        self.assertEqual(plan_cost(["videos.insert", "thumbnails.set", "captions.insert"]), {"units": 450, "uploads": 1})
        with self.assertRaises(PipelineError):
            plan_cost(["videos.delete"])
        packet = audit_packet(YouTubeSettings(episodes_per_day=2, shorts_per_episode=3))
        self.assertEqual(packet["quota_per_episode"], {"units": 450, "uploads": 4})
        self.assertEqual(packet["estimated_daily_usage"], {"units": 900, "uploads": 8})
        self.assertTrue(packet["within_default_quota"])
        self.assertEqual(packet["scopes"], list(SCOPES))
        json.dumps(packet)


if __name__ == "__main__":
    unittest.main()
