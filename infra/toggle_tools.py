"""Temporarily redeploy every roster agent WITHOUT tools (file_search/web_search), so
real production can proceed around a confirmed Foundry platform bug: any tool
invocation (file_search or web_search) by an agent_reference call 403s with
'Identity(object id: 098da25e-70f1-4803-93e6-cc577acaafe8) does not have permissions
for .../agents/write', even after granting that identity Foundry User at account
scope and waiting >10 minutes for propagation. See
docs/studio-quality-review-2026-10-09.md. Restore with --restore afterward.
"""
import sys
from dataclasses import replace

sys.path.insert(0, "src")
from story_pipeline.studio.config import load_config
from story_pipeline.studio.deploy_agents import connect, deploy_agents
from story_pipeline.studio.playbook import Playbook
from story_pipeline.studio.roster import ROSTER

config = load_config()
project, openai_client = connect(config)
playbook = Playbook(config.channel_dir / "playbook.json")

mode = sys.argv[1] if len(sys.argv) > 1 else "strip"
roster = tuple(replace(spec, tools=()) for spec in ROSTER) if mode == "strip" else ROSTER
manifest = deploy_agents(project, openai_client, config, roster=roster,
                         manifest_path=config.channel_dir.parent / "agents-manifest.json")
print(mode, "changed:", sum(1 for a in manifest["agents"].values() if a["changed"]), "/", len(manifest["agents"]))
