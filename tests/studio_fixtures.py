"""Shared builders for studio tests (not a test module)."""
from __future__ import annotations

import io
import math
import re
import struct
import wave
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from story_pipeline.studio.config import StudioConfig

NARRATIONS = [
    "민지는 문을 열었다. 바람이 차갑게 들어왔다.",
    "준호는 <편지>를 읽었다 & 웃었다. 오래된 약속이 떠올랐다.",
    "두 사람은 식탁에 앉았다. 국이 천천히 식어 갔다.",
    "민지는 창문을 닫았다. 마침내 집 안이 따뜻해졌다.",
]
ANCHORS = [("민지는 문을", "바람이 차갑게"), ("준호는 편지를", "오래된 약속이"),
           ("두 사람은 식탁에", "국이 천천히"), ("민지는 창문을", "마침내 집 안이")]
MOODS = ["tender", "warm", "sad", "hopeful"]


def make_config(**overrides) -> StudioConfig:
    values = dict(
        project_endpoint="https://example.services.ai.azure.com/api/projects/test",
        speech_endpoint="https://example.cognitiveservices.azure.com/", speech_region="koreacentral",
        image_deployment="gpt-image-1", episode_budget_usd=Decimal("10"),
        agent_models={"art-critic": "gpt-5-mini"}, model_prices={},
        image_usd_per_call=Decimal("0.07"), speech_usd_per_million_characters=Decimal("15"),
    )
    values.update(overrides)
    return StudioConfig(**values)


def make_episode() -> dict:
    scenes = []
    for index, (narration, anchors) in enumerate(zip(NARRATIONS, ANCHORS), start=1):
        scenes.append({
            "id": f"{index:02}", "title": f"장면 {index}", "pov": "minji" if index % 2 else "junho",
            "location": "home", "mood": MOODS[index - 1], "narration": narration,
            "shots": [
                {"id": "01", "anchor": anchors[0], "characters": ["minji"] if index % 2 else ["junho", "minji"],
                 "visual_prompt": "현관 앞", "emotion": "concerned", "reason": "시작", "sfx": ["door"] if index == 1 else []},
                {"id": "02", "anchor": anchors[1], "characters": [], "visual_prompt": "빈 방",
                 "emotion": "neutral", "reason": "여운", "sfx": []},
            ],
        })
    return {
        "schema_version": 3, "id": "test_episode", "title": "시험 에피소드", "subtitle": "부제",
        "logline": "로그라인", "fiction_notice": "창작 이야기", "synopsis": "두 사람이 집에서 화해한다.",
        "characters": [
            {"id": "minji", "name": "민지", "age_band": "50대", "gender": "female", "adult": True,
             "appearance": "짧은 회색 머리, 초록 카디건",
             "voice": {"name": "ko-KR-SunHiNeural", "style": "sad", "rate": "+0%", "pitch": "-2%"}},
            {"id": "junho", "name": "준호", "age_band": "20대", "gender": "male", "adult": True,
             "appearance": "검은 머리, 남색 점퍼",
             "voice": {"name": "ko-KR-InJoonNeural", "style": "default", "rate": "+5%", "pitch": "+0%"}},
        ],
        "locations": [{"id": "home", "description": "작은 아파트 거실"}],
        "scenes": scenes,
    }


def wav_bytes(seconds: float, *, rate: int = 24000, frequency: float = 220.0, amplitude: int = 6000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        frames = int(seconds * rate)
        audio.writeframes(b"".join(
            struct.pack("<h", int(amplitude * math.sin(2 * math.pi * frequency * i / rate))) for i in range(frames)
        ))
    return buffer.getvalue()


def write_wav(path: Path, seconds: float, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav_bytes(seconds, **kwargs))
    return path


def png_bytes(color: str = "#336699", size: tuple[int, int] = (96, 64)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeSynthesizer:
    def __init__(self, *, drop_last: bool = False, sample_rate: int = 24000, fail: bool = False):
        self.calls: list[str] = []
        self.drop_last, self.sample_rate, self.fail = drop_last, sample_rate, fail

    def synthesize(self, ssml: str):
        self.calls.append(ssml)
        if self.fail:
            raise RuntimeError("service unavailable")
        text = "".join(ET.fromstring(ssml).itertext())
        sentences = [s for s in re.split(r"(?<=\.)\s+", text) if s.strip()]
        if self.drop_last:
            sentences = sentences[:-1]
        boundaries = [{"type": "SentenceBoundary", "offset": int((0.1 + 2 * i) * 1e7), "duration": int(1.8e7),
                       "text": sentence} for i, sentence in enumerate(sentences)]
        return wav_bytes(2 * len(sentences) + 0.2, rate=self.sample_rate), boundaries


def make_speech(root: Path, episode: dict) -> list[dict]:
    from story_pipeline.studio.ledger import CostLedger
    from story_pipeline.studio.speech_azure import synthesize_episode_azure
    ledger = CostLedger(root / "speech-ledger.json", Decimal("10"))
    return synthesize_episode_azure(episode, root / "scenes", config=make_config(), ledger=ledger,
                                    synthesizer_factory=lambda config: FakeSynthesizer())

def make_libraries(root: Path) -> tuple[Path, Path]:
    import json
    music, sfx = root / "music", root / "sfx"
    write_wav(music / "calm.wav", 3.0, rate=44100, frequency=330)
    write_wav(music / "bright.wav", 2.5, rate=44100, frequency=440)
    (music / "LICENSE-calm.txt").write_text("CC0", encoding="utf-8")
    licensed = {"source": "test", "license_id": "CC0-1.0", "commercial_use": True}
    (music / "library.json").write_text(json.dumps({"entries": [
        {"file": "calm.wav", "moods": ["tender", "sad", "warm", "hopeful"], "attribution": "Calm by Tester (CC0)",
         "license": {**licensed, "proof_url_or_file": "LICENSE-calm.txt"}},
        {"file": "bright.wav", "moods": ["hopeful", "warm"], "attribution": "",
         "license": {**licensed, "proof_url_or_file": "https://example.com/license"}},
    ]}), encoding="utf-8")
    write_wav(sfx / "door.wav", 0.4, rate=48000, frequency=120, amplitude=12000)
    (sfx / "library.json").write_text(json.dumps([
        {"file": "door.wav", "tags": ["door"], "attribution": "Door by Tester",
         "license": {**licensed, "proof_url_or_file": "https://example.com/door"}},
    ]), encoding="utf-8")
    return music, sfx


def make_frames(directory: Path, episode: dict) -> Path:
    from PIL import Image
    directory.mkdir(parents=True, exist_ok=True)
    for index, (scene, shot) in enumerate((s, h) for s in episode["scenes"] for h in s["shots"]):
        Image.new("RGB", (480, 270), (40 + index * 20, 90, 160 - index * 10)).save(
            directory / f"{scene['id']}-shot-{shot['id']}.png")
    return directory
