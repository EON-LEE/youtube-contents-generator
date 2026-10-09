"""Real production run using the committed FoundryTransport directly (no workarounds) —
now that WSL az login is restored to admin@m365cpi74210306.onmicrosoft.com."""
import sys

sys.path.insert(0, "src")
from story_pipeline.studio import cli as studio_cli
from story_pipeline.studio.config import load_config
from story_pipeline.studio.gateway import FoundryTransport

if __name__ == "__main__":
    import json
    config = load_config()
    transport = FoundryTransport(config.project_endpoint)
    run_id = sys.argv[1] if len(sys.argv) > 1 else "ep-2026-10-09-05"
    result = studio_cli.command_job(run_id, transport=transport)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
