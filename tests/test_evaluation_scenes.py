from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from story_pipeline import evaluation
from story_pipeline.models import PipelineError
from studio_fixtures import write_wav

FFMPEG = importlib.util.find_spec("imageio_ffmpeg") is not None


class FakeModel:
    def __init__(self):
        self.clips = []

    def transcribe(self, path, **kwargs):
        self.clips.append(Path(path).name)
        return iter([SimpleNamespace(text="안녕하세요 반갑습니다")]), SimpleNamespace(language="ko")


class SceneSelectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".asr-{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        for index in range(1, 13):
            identifier = f"{index:02}"
            write_wav(self.root / "scenes" / f"{identifier}.wav", 2.0)
            (self.root / "scenes" / f"{identifier}.speech.json").write_text(json.dumps({
                "voice": "ko-KR-SunHiNeural",
                "captions": [{"start": 0.1, "end": 1.8, "text": "안녕하세요. 반갑습니다."}],
            }), encoding="utf-8")

    def test_selection_modes(self):
        self.assertEqual(evaluation.select_scenes(self.root, evaluation.DEFAULT_SCENES), ["01", "03", "11"])
        self.assertEqual(len(evaluation.select_scenes(self.root, None)), 12)
        self.assertEqual(evaluation.select_scenes(self.root, 3), ["01", "07", "12"])
        self.assertEqual(evaluation.select_scenes(self.root, 1), ["01"])
        with self.assertRaisesRegex(PipelineError, "available"):
            evaluation.select_scenes(self.root, ["99"])

    @unittest.skipUnless(FFMPEG, "FFmpeg is required to cut evaluation clips.")
    def test_evaluates_all_scenes_with_injected_model(self):
        model = FakeModel()
        report = evaluation.evaluate_speech(self.root, scenes=None, model_factory=lambda: model)
        self.assertEqual(report["scenes"], [f"{i:02}" for i in range(1, 13)])
        self.assertEqual(len(model.clips), 12)
        self.assertEqual(report["weighted_normalized_character_error_rate"], 0.0)
        self.assertTrue((self.root / "speech-evaluation.json").is_file())


if __name__ == "__main__":
    unittest.main()
