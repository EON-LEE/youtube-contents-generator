from __future__ import annotations

import json
import time
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from ..models import PipelineError

MICRO = Decimal("0.000001")


class BudgetExceeded(PipelineError):
    """Raised before a call whose reserved cost would exceed the episode budget."""


def ceil_usd(amount: Decimal) -> Decimal:
    return amount.quantize(MICRO, rounding=ROUND_CEILING)


class CostLedger:
    """Real-money ledger for one episode run.

    Every billable call reserves its worst-case cost first, then settles with
    measured usage. Unsettled reservations count against the budget, so an
    interrupted run can never silently overspend on resume.
    """

    def __init__(self, path: Path, budget_usd: Decimal):
        self.path = path
        self.budget = Decimal(budget_usd)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if Decimal(data["budget_usd"]) != self.budget:
                raise PipelineError("Ledger budget differs from configuration; start a new run.")
            self.entries: list[dict[str, Any]] = data["entries"]
        else:
            self.entries = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "budget_usd": str(self.budget), "entries": self.entries,
            "committed_usd": str(self.committed()), "settled_usd": str(self.settled()),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def committed(self) -> Decimal:
        total = Decimal(0)
        for entry in self.entries:
            total += Decimal(entry["actual_usd"] if entry["status"] == "settled" else entry["reserved_usd"])
        return total

    def settled(self) -> Decimal:
        return sum((Decimal(e["actual_usd"]) for e in self.entries if e["status"] == "settled"), Decimal(0))

    def remaining(self) -> Decimal:
        return self.budget - self.committed()

    def reserve(self, label: str, estimate_usd: Decimal, detail: dict[str, Any] | None = None) -> int:
        estimate = ceil_usd(Decimal(estimate_usd))
        if estimate < 0:
            raise PipelineError("Cost estimate cannot be negative.")
        if self.committed() + estimate > self.budget:
            raise BudgetExceeded(
                f"{label}: reserving USD {estimate} would exceed the episode budget "
                f"(committed {self.committed()} of {self.budget}). Run stopped."
            )
        self.entries.append({
            "id": len(self.entries) + 1, "label": label, "status": "reserved",
            "reserved_usd": str(estimate), "actual_usd": "0", "detail": detail or {},
            "reserved_at": time.time(),
        })
        self._save()
        return len(self.entries)

    def settle(self, entry_id: int, actual_usd: Decimal, usage: dict[str, Any]) -> None:
        entry = self._entry(entry_id)
        entry.update({
            "status": "settled", "actual_usd": str(ceil_usd(Decimal(actual_usd))),
            "usage": usage, "settled_at": time.time(),
        })
        self._save()

    def fail(self, entry_id: int, error: str) -> None:
        """A failed call keeps its reservation: providers may still bill it."""
        entry = self._entry(entry_id)
        entry.update({"status": "failed", "error": error[:2000]})
        self._save()

    def _entry(self, entry_id: int) -> dict[str, Any]:
        if not 1 <= entry_id <= len(self.entries) or self.entries[entry_id - 1]["status"] != "reserved":
            raise PipelineError(f"Ledger entry {entry_id} is not an open reservation.")
        return self.entries[entry_id - 1]

    def summary(self) -> dict[str, Any]:
        by_label: dict[str, Decimal] = {}
        for entry in self.entries:
            key = entry["label"].split(":", 1)[0]
            value = Decimal(entry["actual_usd"] if entry["status"] == "settled" else entry["reserved_usd"])
            by_label[key] = by_label.get(key, Decimal(0)) + value
        return {
            "budget_usd": str(self.budget), "committed_usd": str(self.committed()),
            "settled_usd": str(self.settled()), "remaining_usd": str(self.remaining()),
            "calls": len(self.entries), "by_stage_usd": {k: str(v) for k, v in sorted(by_label.items())},
            "note": "Failed calls keep their reservation conservatively; reconcile with Azure Cost Management.",
        }
