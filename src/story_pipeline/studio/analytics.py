"""YouTube Analytics API v2 collection into an idempotent per-video, per-day store."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from ..models import PipelineError

CORE_METRICS = ("views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
                "likes", "subscribersGained")
# Thumbnail impressions/CTR are not on https://developers.google.com/youtube/analytics/metrics and
# reports.query rejects videoThumbnailImpressions* with 400 "The query is not supported"
# (issuetracker.google.com/issues/489579961). They exist only in the bulk Reporting API
# (channel_reach_basic_a1). Add names here if reports.query starts supporting them.
IMPRESSION_METRICS: tuple[str, ...] = ()
METRICS = CORE_METRICS + IMPRESSION_METRICS
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class AnalyticsStore(Protocol):
    def upsert(self, rows: Iterable[Mapping[str, Any]]) -> int: ...
    def rows(self, video_id: str | None = None) -> list[dict[str, Any]]: ...


class JsonAnalyticsStore:
    """Rows keyed by (video_id, date); rewriting a day replaces it."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1:
                raise PipelineError(f"Unsupported analytics store schema in {path}.")
            self._rows: dict[str, dict[str, Any]] = data["rows"]
        else:
            self._rows = {}

    def upsert(self, rows: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for row in rows:
            self._rows[f"{row['video_id']}|{row['date']}"] = dict(row)
            count += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 1, "rows": dict(sorted(self._rows.items()))},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)
        return count

    def rows(self, video_id: str | None = None) -> list[dict[str, Any]]:
        return [dict(r) for _, r in sorted(self._rows.items()) if video_id is None or r["video_id"] == video_id]


class TableAnalyticsStore:
    """Azure Table Storage: PartitionKey=video_id, RowKey=date. Requires azure-data-tables."""

    def __init__(self, table_client: Any):
        self.table = table_client

    @classmethod
    def connect(cls, endpoint: str, table_name: str = "analytics") -> "TableAnalyticsStore":
        try:
            from azure.data.tables import TableServiceClient
            from azure.identity import DefaultAzureCredential
        except ImportError as error:
            raise PipelineError("Install azure-data-tables and azure-identity for the Table store.") from error
        service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
        return cls(service.create_table_if_not_exists(table_name))

    def upsert(self, rows: Iterable[Mapping[str, Any]]) -> int:
        count = 0
        for row in rows:
            self.table.upsert_entity({"PartitionKey": row["video_id"], "RowKey": row["date"], **row})
            count += 1
        return count

    def rows(self, video_id: str | None = None) -> list[dict[str, Any]]:
        entities = (self.table.query_entities(f"PartitionKey eq '{video_id.replace(chr(39), chr(39) * 2)}'")
                    if video_id else self.table.list_entities())
        return sorted(({k: v for k, v in dict(e).items() if k not in ("PartitionKey", "RowKey")} for e in entities),
                      key=lambda r: (r["video_id"], r["date"]))


def _check_dates(start: str, end: str) -> None:
    for value in (start, end):
        if not isinstance(value, str) or not _DATE.match(value):
            raise PipelineError(f"Dates must be YYYY-MM-DD, got {value!r}.")
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise PipelineError(f"Start date {start} is after end date {end}.")


def query_video(service: Any, video_id: str, start: str, end: str,
                metrics: Sequence[str] = METRICS) -> list[dict[str, Any]]:
    try:
        response = service.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, metrics=",".join(metrics),
            dimensions="day", filters=f"video=={video_id}", sort="day").execute()
    except Exception as error:
        raise PipelineError(f"Analytics query for video {video_id} failed: {error}") from error
    headers = [h["name"] for h in response.get("columnHeaders", [])]
    missing = {"day", *metrics} - set(headers)
    if missing:
        raise PipelineError(f"Analytics response for {video_id} lacks columns {sorted(missing)}.")
    return [dict(zip(headers, values)) for values in response.get("rows") or []]


def collect(service: Any, store: AnalyticsStore, videos: Sequence[str | Mapping[str, Any]], start: str, end: str,
            metrics: Sequence[str] = METRICS) -> dict[str, Any]:
    """Collects daily metrics per video and upserts them; safe to re-run for overlapping ranges.
    ``videos`` items are video ids or ``{"video_id", "episode"?}``. YouTube data lags 2-3 days."""
    _check_dates(start, end)
    collected_at = datetime.now(timezone.utc).isoformat()
    per_video: dict[str, int] = {}
    for video in videos:
        entry = {"video_id": video} if isinstance(video, str) else dict(video)
        video_id = entry.get("video_id")
        if not video_id:
            raise PipelineError(f"Video entry without video_id: {video!r}")
        rows = []
        for raw in query_video(service, video_id, start, end, metrics):
            row = {"video_id": video_id, "date": raw.pop("day"), **raw, "collected_at": collected_at}
            if entry.get("episode"):
                row["episode"] = entry["episode"]
            rows.append(row)
        per_video[video_id] = store.upsert(rows)
    return {"start": start, "end": end, "metrics": list(metrics), "rows_upserted": per_video,
            "total_rows": sum(per_video.values())}


def summarize(rows: Iterable[Mapping[str, Any]], video_id: str | None = None) -> dict[str, Any]:
    """Totals plus view-weighted retention (averageViewPercentage / 100). ctr stays None unless
    impression metrics are collected."""
    selected = [r for r in rows if video_id is None or r["video_id"] == video_id]
    views = sum(int(r.get("views", 0)) for r in selected)
    minutes = sum(float(r.get("estimatedMinutesWatched", 0)) for r in selected)
    weighted = sum(float(r.get("averageViewPercentage", 0)) * int(r.get("views", 0)) for r in selected)
    impressions = sum(int(r.get("impressions", 0)) for r in selected)
    clicks = sum(float(r.get("impressionClickThroughRate", 0)) * int(r.get("impressions", 0)) for r in selected)
    return {
        "video_id": video_id, "days": len(selected), "views": views, "watch_hours": round(minutes / 60, 3),
        "likes": sum(int(r.get("likes", 0)) for r in selected),
        "subscribers_gained": sum(int(r.get("subscribersGained", 0)) for r in selected),
        "retention": round(weighted / views / 100, 4) if views else None,
        "ctr": round(clicks / impressions / 100, 4) if impressions else None,
    }
