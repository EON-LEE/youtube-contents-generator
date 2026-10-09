"""Generate placeholder, self-authored (no third-party rights) music/SFX for a real
production smoke test. See library/LICENSE-PROOF-SYNTHETIC.md."""
import json
import subprocess
import sys

sys.path.insert(0, "src")
from story_pipeline.media import ffmpeg_executable

FFMPEG = ffmpeg_executable()

# mood -> (base frequency Hz, duration s)
MOODS = {
    "tender": 220, "concerned": 196, "tense": 174, "sad": 165,
    "hopeful": 262, "warm": 233, "neutral": 207, "relieved": 246,
}
# sfx tag -> (noise color, duration s)
SFX = {
    "door": ("brown", 1.0), "rain": ("pink", 8.0), "footsteps": ("brown", 1.5),
    "kettle": ("white", 3.0), "phone_vibrate": ("brown", 0.8), "bus": ("brown", 4.0),
    "birds": ("pink", 3.0), "wind": ("pink", 6.0), "dishes": ("white", 1.2),
    "cart": ("brown", 2.0), "chair": ("brown", 0.8), "chair_creak": ("brown", 0.8),
    "clock_tick": ("white", 2.0), "cough": ("brown", 0.6), "digital_beep": ("white", 0.3),
    "distant_footsteps": ("brown", 2.0), "distant_voices": ("pink", 3.0),
    "elevator_ding": ("white", 0.5), "engine_idle": ("brown", 4.0), "hospital_ambience": ("pink", 6.0),
    "keyboard": ("white", 1.5), "knock": ("brown", 0.5), "muffled_ambience": ("pink", 5.0),
    "murmur": ("pink", 3.0), "office_murmur": ("pink", 4.0), "paper_cup": ("white", 0.6),
    "paper_rustle": ("white", 1.0), "papers": ("white", 1.0), "pen": ("white", 0.4),
    "phone_ring": ("white", 1.5), "phone_set_down": ("brown", 0.5), "plastic_bag_rustle": ("white", 1.0),
    "water_dispenser": ("brown", 1.5),
}


def run(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"ffmpeg failed: {result.stderr[-2000:]}")


music_entries = []
for mood, freq in MOODS.items():
    name = f"{mood}.wav"
    run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration=40,volume=0.12",
         "-c:a", "pcm_s16le", "-ar", "44100", f"library/music/{name}"])
    music_entries.append({
        "file": name, "moods": [mood], "attribution": "",
        "license": {"source": "synthetic (FFmpeg sine generator)", "license_id": "no-third-party-rights",
                    "proof_url_or_file": "LICENSE-PROOF-SYNTHETIC.md", "commercial_use": True},
    })
json.dump({"entries": music_entries}, open("library/music/library.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

sfx_entries = []
for tag, (color, duration) in SFX.items():
    name = f"{tag}.wav"
    run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", f"anoisesrc=color={color}:duration={duration}:seed=7,volume=0.35",
         "-c:a", "pcm_s16le", "-ar", "44100", f"library/sfx/{name}"])
    sfx_entries.append({
        "file": name, "tags": [tag], "attribution": "",
        "license": {"source": "synthetic (FFmpeg noise generator)", "license_id": "no-third-party-rights",
                    "proof_url_or_file": "LICENSE-PROOF-SYNTHETIC.md", "commercial_use": True},
    })
json.dump({"entries": sfx_entries}, open("library/sfx/library.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

print("music:", len(music_entries), "sfx:", len(sfx_entries))

