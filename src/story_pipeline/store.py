from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .models import APPROVALS, STAGES, JobSpec, PipelineError, canonical, digest, required_text, usd


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "workflow.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT
            );
            CREATE TABLE IF NOT EXISTS stages (
                job_id TEXT NOT NULL REFERENCES jobs(id), name TEXT NOT NULL,
                instruction TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                fingerprint TEXT, artifact_path TEXT, artifact_hash TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
                PRIMARY KEY(job_id, name)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                stage TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                reserved_micros INTEGER NOT NULL, simulated_actual_micros INTEGER NOT NULL DEFAULT 0,
                actual_external_micros INTEGER NOT NULL DEFAULT 0,
                estimate_json TEXT NOT NULL, error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                kind TEXT NOT NULL, scope_hash TEXT NOT NULL, actor TEXT NOT NULL,
                simulated INTEGER NOT NULL CHECK (simulated=1),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with (self.root / "worker.lock").open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise PipelineError("Another process owns this workspace. Wait for it to finish.") from error
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(self, spec: JobSpec) -> None:
        spec.validate()
        with self.lock(), self.db:
            if self.db.execute("SELECT id FROM jobs WHERE id=?", (spec.episode_id,)).fetchone():
                raise PipelineError("Episode already exists. Use revise or choose another ID.")
            self.db.execute("INSERT INTO jobs(id,spec) VALUES (?,?)", (spec.episode_id, canonical(asdict(spec))))
            self.db.executemany(
                "INSERT INTO stages(job_id,name) VALUES (?,?)",
                [(spec.episode_id, name) for name in STAGES],
            )

    def spec(self, job_id: str) -> JobSpec:
        row = self.db.execute("SELECT spec FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise PipelineError(f"Unknown episode: {job_id}")
        spec = JobSpec(**json.loads(row["spec"]))
        spec.validate()
        return spec

    def stage(self, job_id: str, name: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM stages WHERE job_id=? AND name=?", (job_id, name)).fetchone()
        if row is None:
            raise PipelineError(f"Unknown episode or stage: {job_id}/{name}")
        return row

    def set_status(self, job_id: str, status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET status=?,error=? WHERE id=?", (status, error, job_id))

    def invalidate_from(self, job_id: str, name: str) -> None:
        names = STAGES[STAGES.index(name):]
        with self.db:
            self.db.executemany(
                """UPDATE stages SET status='pending',fingerprint=NULL,artifact_path=NULL,
                   artifact_hash=NULL,attempts=0,error=NULL WHERE job_id=? AND name=?""",
                [(job_id, stage) for stage in names],
            )
            self.db.execute("UPDATE jobs SET status='pending',error=NULL WHERE id=?", (job_id,))

    def revise(self, job_id: str, name: str, instruction: str) -> None:
        required_text(instruction, "Revision instruction")
        if len(instruction) > 5000 or name not in STAGES:
            raise PipelineError("Invalid stage or revision instruction exceeds 5,000 characters.")
        with self.lock():
            row = self.stage(job_id, name)
            if row["instruction"] == instruction:
                raise PipelineError("Instruction is unchanged; nothing to revise.")
            if self.running(job_id):
                raise PipelineError("Interrupted work exists. Resume with --recover before revising.")
            with self.db:
                self.db.execute(
                    "UPDATE stages SET instruction=? WHERE job_id=? AND name=?",
                    (instruction, job_id, name),
                )
                self.invalidate_from(job_id, name)

    def read_artifact(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["status"] != "done" or not row["artifact_path"]:
            raise PipelineError(f"{row['name']}: no completed artifact.")
        path = (self.root / row["artifact_path"]).resolve()
        if not path.is_relative_to(self.root):
            raise PipelineError("Artifact path escapes the workspace.")
        try:
            data = path.read_bytes()
        except OSError as error:
            raise PipelineError(f"Cannot read artifact {row['name']}: {error}") from error
        if hashlib.sha256(data).hexdigest() != row["artifact_hash"]:
            raise PipelineError(f"{row['name']}: artifact integrity failure. Review and explicitly revise.")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError) as error:
            raise PipelineError(f"{row['name']}: invalid artifact JSON.") from error
        if (
            not isinstance(value, dict)
            or value.get("simulated") is not True
            or value.get("episode_id") != row["job_id"]
            or value.get("stage") != row["name"]
            or value.get("input_hash") != row["fingerprint"]
        ):
            raise PipelineError("Artifact identity does not match the stored stage.")
        return value

    def scope(self, job_id: str, kind: str) -> str:
        if kind not in APPROVALS:
            raise PipelineError("Approval kind must be script or review.")
        stop = "direct" if kind == "script" else "qa"
        hashes = []
        for stage in STAGES[:STAGES.index(stop) + 1]:
            row = self.stage(job_id, stage)
            self.read_artifact(row)
            hashes.append(row["artifact_hash"])
        return digest({"job": job_id, "kind": kind, "artifact_chain": hashes, "simulated": True})

    def approved(self, job_id: str, kind: str) -> bool:
        scope_hash = self.scope(job_id, kind)
        return self.db.execute(
            "SELECT 1 FROM approvals WHERE job_id=? AND kind=? AND scope_hash=?",
            (job_id, kind, scope_hash),
        ).fetchone() is not None

    def approve(self, job_id: str, kind: str, actor: str) -> str:
        required_text(actor, "Reviewer")
        if len(actor) > 100:
            raise PipelineError("Reviewer must be at most 100 characters.")
        with self.lock(), self.db:
            scope_hash = self.scope(job_id, kind)
            self.db.execute(
                "INSERT INTO approvals(job_id,kind,scope_hash,actor,simulated) VALUES (?,?,?,?,1)",
                (job_id, kind, scope_hash, actor),
            )
        return scope_hash

    def running(self, job_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM attempts WHERE job_id=? AND status='running'", (job_id,)
        ).fetchall()

    def recover(self, job_id: str) -> None:
        for attempt in self.running(job_id):
            self.fail(attempt["id"], "interrupted", "Explicit recovery of interrupted simulated attempt.")

    def reserve(self, spec: JobSpec, stage: str, fingerprint: str, quote: dict[str, Any]) -> int:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            total = self.db.execute(
                """SELECT COALESCE(SUM(reserved_micros+simulated_actual_micros),0)
                   FROM attempts WHERE job_id=?""", (spec.episode_id,)
            ).fetchone()[0]
            if total + quote["total_micros"] > spec.simulation_budget_micros:
                raise PipelineError("Virtual USD budget exceeded. No external spending occurred.")
            cursor = self.db.execute(
                """INSERT INTO attempts(job_id,stage,fingerprint,status,reserved_micros,estimate_json)
                   VALUES (?,?,?,'running',?,?)""",
                (spec.episode_id, stage, fingerprint, quote["total_micros"], canonical(quote)),
            )
            attempt_id = cursor.lastrowid
            if attempt_id is None:
                raise PipelineError("Failed to allocate attempt ID.")
            self.db.execute(
                """UPDATE stages SET status='running',fingerprint=?,attempts=attempts+1,error=NULL
                   WHERE job_id=? AND name=?""", (fingerprint, spec.episode_id, stage)
            )
            self.db.commit()
            return attempt_id
        except (PipelineError, sqlite3.Error):
            self.db.rollback()
            raise

    def fail(self, attempt_id: int, status: str, error: str) -> None:
        with self.db:
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise PipelineError("Unknown attempt.")
            self.db.execute(
                """UPDATE attempts SET status=?,error=?,simulated_actual_micros=reserved_micros,
                   reserved_micros=0 WHERE id=?""", (status, error, attempt_id)
            )
            self.db.execute(
                "UPDATE stages SET status='failed',error=? WHERE job_id=? AND name=?",
                (error, row["job_id"], row["stage"]),
            )

    def finish(self, attempt_id: int, artifact: dict[str, Any]) -> None:
        relative = Path("artifacts") / artifact["episode_id"] / f"{artifact['stage']}-{attempt_id}.json"
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise PipelineError("Artifact destination escapes the workspace.")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = canonical(artifact).encode("utf-8")
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        with self.db:
            self.db.execute(
                """UPDATE attempts SET status='done',simulated_actual_micros=reserved_micros,
                   reserved_micros=0 WHERE id=?""", (attempt_id,)
            )
            self.db.execute(
                """UPDATE stages SET status='done',artifact_path=?,artifact_hash=?,error=NULL
                   WHERE job_id=? AND name=?""",
                (str(relative), hashlib.sha256(content).hexdigest(), artifact["episode_id"], artifact["stage"]),
            )

    def report(self, job_id: str) -> dict[str, Any]:
        spec = self.spec(job_id)
        job = self.db.execute("SELECT status,error FROM jobs WHERE id=?", (job_id,)).fetchone()
        stages = [dict(self.stage(job_id, stage)) for stage in STAGES]
        attempts = [dict(row) for row in self.db.execute(
            "SELECT * FROM attempts WHERE job_id=? ORDER BY id", (job_id,)
        )]
        approvals = [dict(row) for row in self.db.execute(
            "SELECT * FROM approvals WHERE job_id=? ORDER BY id", (job_id,)
        )]
        return {
            "episode_id": job_id,
            "simulated": True,
            "status": job["status"],
            "error": job["error"],
            "target_minutes": spec.target_minutes,
            "actual_duration_seconds": None,
            "media_created": False,
            "stages": stages,
            "attempts": attempts,
            "approval_history": approvals,
            "costs": {
                "currency": "USD",
                "virtual_budget_usd": usd(spec.simulation_budget_micros),
                "virtual_reserved_usd": usd(sum(row["reserved_micros"] for row in attempts)),
                "virtual_consumed_usd": usd(sum(row["simulated_actual_micros"] for row in attempts)),
                "actual_external_spend_usd": usd(sum(row["actual_external_micros"] for row in attempts)),
                "limitations": "Virtual ledger charges estimates even for failed attempts. No real usage.",
            },
            "production_ready": False,
            "publishing_available": False,
        }
