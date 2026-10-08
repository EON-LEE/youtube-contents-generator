"""Durable studio state shared by the hosted agent and the jobs: playbook, uploaded
video registry, YouTube quota ledger, analytics, calibration. Local directory,
mirrored to blob storage (``STUDIO_STORAGE_ACCOUNT_URL``, container ``state``).

Writers use ``transaction()``: take a blob lease, re-pull, mutate, push, release.
Critical sections are short, so a long render never blocks analytics or learning,
and two jobs can never overwrite each other's videos, lessons or quota."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from ..models import PipelineError

STATE_FILES = ("playbook.json", "videos.json", "youtube-quota.json", "analytics.json", "calibration.json",
               "knowledge-state.json")
LOCK_BLOB = "_lock"
STALE_LOCK_SECONDS = 2 * 3600


def _blob_container(account_url: str, container: str):
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import ContainerClient
    except ImportError as error:
        raise PipelineError("STUDIO_STORAGE_ACCOUNT_URL is set; install azure-storage-blob.") from error
    client = ContainerClient(account_url, container, credential=DefaultAzureCredential())
    if not client.exists():
        raise PipelineError(f"Blob container {container!r} does not exist at {account_url}; deploy infra/main.bicep.")
    return client


class StateStore:
    def __init__(self, root: Path, environ: Mapping[str, str] | None = None, container_client=None,
                 clock=time.time, sleep=time.sleep):
        environ = os.environ if environ is None else environ
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.account_url = environ.get("STUDIO_STORAGE_ACCOUNT_URL", "").strip()
        self.container = environ.get("STUDIO_STATE_CONTAINER", "state")
        self._client = container_client
        self._seed: Path | None = None
        self.clock = clock
        self.sleep = sleep

    def path(self, name: str) -> Path:
        if name not in STATE_FILES:
            raise PipelineError(f"Unknown state file {name!r}.")
        return self.root / name

    def _container(self):
        if self._client is None and self.account_url:
            self._client = _blob_container(self.account_url, self.container)
        return self._client

    def pull(self, seed_playbook: Path | None = None) -> None:
        """Fetch remote state; seed the playbook from the repository on first use."""
        self._seed = seed_playbook or self._seed
        client = self._container()
        if client is not None:
            existing = {blob.name for blob in client.list_blobs()}
            for name in STATE_FILES:
                if name in existing:
                    self.path(name).write_bytes(client.download_blob(name).readall())
        playbook = self.path("playbook.json")
        if not playbook.exists() and self._seed is not None and self._seed.exists():
            playbook.write_bytes(self._seed.read_bytes())

    def push(self) -> int:
        client = self._container()
        if client is None:
            return 0
        count = 0
        for name in STATE_FILES:
            path = self.path(name)
            if path.exists():
                with path.open("rb") as handle:
                    client.upload_blob(name, handle, overwrite=True)
                count += 1
        return count

    @contextmanager
    def transaction(self, wait_seconds: float = 1800) -> Iterator["StateStore"]:
        client = self._container()
        if client is None:
            self.pull()
            yield self
            return
        lease = self._acquire(client, wait_seconds)
        try:
            self.pull()
            yield self
            self.push()
        finally:
            lease.release()

    def _acquire(self, client, wait_seconds: float):
        from azure.core.exceptions import HttpResponseError, ResourceExistsError
        blob = client.get_blob_client(LOCK_BLOB)
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        deadline = self.clock() + wait_seconds
        while True:
            try:
                lease = blob.acquire_lease(lease_duration=-1)
                blob.set_blob_metadata({"acquired_at": str(int(self.clock()))}, lease=lease)
                return lease
            except HttpResponseError as error:
                if getattr(error, "status_code", None) != 409:
                    raise
            acquired = int((blob.get_blob_properties().metadata or {}).get("acquired_at", "0") or 0)
            if acquired and self.clock() - acquired > STALE_LOCK_SECONDS:
                blob.break_lease(lease_break_period=0)
                continue
            if self.clock() > deadline:
                raise PipelineError("Studio state is locked by another job; try again later.")
            self.sleep(15)


def sync_library(target: Path, environ: Mapping[str, str] | None = None, container_client=None) -> int:
    """Download the licensed music/SFX library (blob container ``library``) for mixing."""
    environ = os.environ if environ is None else environ
    account = environ.get("STUDIO_STORAGE_ACCOUNT_URL", "").strip()
    client = container_client or (_blob_container(account, environ.get("STUDIO_LIBRARY_CONTAINER", "library"))
                                  if account else None)
    if client is None:
        return 0
    count = 0
    root = target.resolve()
    for blob in client.list_blobs():
        path = (root / blob.name).resolve()
        if root not in path.parents:
            raise PipelineError(f"Refusing library blob outside the library directory: {blob.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(client.download_blob(blob.name).readall())
        count += 1
    return count
