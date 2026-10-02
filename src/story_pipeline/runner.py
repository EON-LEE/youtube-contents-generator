from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .costs import DEFAULT_PRICES, Price, estimate
from .models import STAGES, Artifact, JobSpec, PipelineError, TransientFailure, digest
from .providers import FixtureProvider
from .roles import validate_payload
from .store import Store


class Runner:
    def __init__(
        self, store: Store, provider: FixtureProvider | None = None,
        prices: dict[str, Price] | None = None,
    ):
        self.store = store
        self.provider = provider if provider is not None else FixtureProvider()
        self.prices = prices if prices is not None else DEFAULT_PRICES

    def run(self, job_id: str, *, dry_run: bool, recover: bool = False) -> dict[str, Any]:
        if not dry_run or self.provider.simulated is not True:
            raise PipelineError("Only explicit simulated dry-run is permitted. Live execution is unavailable.")
        with self.store.lock():
            spec = self.store.spec(job_id)
            if self.store.running(job_id):
                if not recover:
                    raise PipelineError("Interrupted attempt found. Inspect status, then resume --recover.")
                self.store.recover(job_id)
            self.store.set_status(job_id, "running")
            try:
                return self._execute(spec)
            except PipelineError as error:
                self.store.set_status(job_id, "blocked", str(error))
                raise

    def _execute(self, spec: JobSpec) -> dict[str, Any]:
        previous = None
        previous_hash = None
        for stage in STAGES:
            row = self.store.stage(spec.episode_id, stage)
            quote = estimate(self.provider.usage(stage, previous), self.prices)
            input_hash = digest({
                "spec": asdict(spec),
                "stage": stage,
                "instruction": row["instruction"],
                "upstream_hash": previous_hash,
                "provider": self.provider.version,
                "estimate": quote,
            })
            if row["fingerprint"] is not None and row["fingerprint"] != input_hash:
                self.store.invalidate_from(spec.episode_id, stage)
                row = self.store.stage(spec.episode_id, stage)
            if row["status"] == "done":
                artifact = self.store.read_artifact(row)
                validate_payload(stage, artifact["payload"])
                previous = artifact["payload"]
                previous_hash = row["artifact_hash"]
                continue
            if stage == "produce" and not self.store.approved(spec.episode_id, "script"):
                self.store.set_status(spec.episode_id, "awaiting_script_approval")
                return self.store.report(spec.episode_id)
            while True:
                row = self.store.stage(spec.episode_id, stage)
                if row["attempts"] >= spec.max_attempts:
                    raise PipelineError(f"{stage}: attempt limit reached. Inspect and explicitly revise.")
                attempt_id = self.store.reserve(spec, stage, input_hash, quote)
                try:
                    payload = self.provider.generate(stage, spec, previous, row["instruction"])
                    validate_payload(stage, payload)
                    artifact = Artifact(spec.episode_id, stage, input_hash, payload)
                    artifact.validate()
                    self.store.finish(attempt_id, artifact.as_dict())
                except TransientFailure as error:
                    self.store.fail(attempt_id, "failed", str(error))
                    continue
                except (PipelineError, OSError) as error:
                    self.store.fail(attempt_id, "failed", str(error))
                    raise PipelineError(f"{stage} failed: {error}") from error
                break
            row = self.store.stage(spec.episode_id, stage)
            previous = payload
            previous_hash = row["artifact_hash"]
        if not self.store.approved(spec.episode_id, "review"):
            self.store.set_status(spec.episode_id, "awaiting_review")
        else:
            self.store.set_status(spec.episode_id, "simulated_complete")
        return self.store.report(spec.episode_id)
