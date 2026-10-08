from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from story_pipeline.models import PipelineError
from story_pipeline.studio.analytics import (
    CORE_METRICS, IMPRESSION_METRICS, METRICS, JsonAnalyticsStore, TableAnalyticsStore, collect, summarize,
)


class FakeAnalytics:
    def __init__(self, data, fail=None):
        self.data, self.fail, self.queries, self.drop = data, fail, [], None

    def reports(self):
        return self

    def query(self, **kwargs):
        self.queries.append(kwargs)
        video = kwargs["filters"].split("==", 1)[1]
        headers = [{"name": "day"}] + [{"name": m} for m in kwargs["metrics"].split(",") if m != self.drop]

        def execute():
            if self.fail:
                raise self.fail
            return {"columnHeaders": headers, "rows": self.data.get(video, [])}
        return SimpleNamespace(execute=execute)


def row(day, views, pct, minutes=60):
    return [day, views, minutes, 300, pct, 3, 1]


class FakeTable:
    def __init__(self):
        self.entities = {}

    def upsert_entity(self, entity):
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def list_entities(self):
        return list(self.entities.values())

    def query_entities(self, flt):
        key = flt.split("'")[1]
        return [e for (pk, _), e in self.entities.items() if pk == key]


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = JsonAnalyticsStore(Path(self.dir.name) / "analytics.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_query_shape_and_metrics(self):
        service = FakeAnalytics({"v1": [row("2026-10-01", 100, 40.0)]})
        collect(service, self.store, ["v1"], "2026-10-01", "2026-10-02")
        query = service.queries[0]
        self.assertEqual(query["ids"], "channel==MINE")
        self.assertEqual(query["dimensions"], "day")
        self.assertEqual(query["filters"], "video==v1")
        self.assertEqual(query["metrics"].split(","), list(METRICS))
        self.assertEqual(IMPRESSION_METRICS, ())
        self.assertIn("averageViewPercentage", CORE_METRICS)

    def test_collect_is_idempotent_upsert(self):
        first = FakeAnalytics({"v1": [row("2026-10-01", 100, 40.0), row("2026-10-02", 50, 50.0)]})
        summary = collect(first, self.store, [{"video_id": "v1", "episode": "ep1"}], "2026-10-01", "2026-10-02")
        self.assertEqual(summary["total_rows"], 2)
        second = FakeAnalytics({"v1": [row("2026-10-02", 80, 50.0), row("2026-10-03", 10, 30.0)]})
        collect(second, self.store, [{"video_id": "v1", "episode": "ep1"}], "2026-10-02", "2026-10-03")
        reloaded = JsonAnalyticsStore(Path(self.dir.name) / "analytics.json").rows("v1")
        self.assertEqual([r["date"] for r in reloaded], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(reloaded[1]["views"], 80)
        self.assertEqual(reloaded[0]["episode"], "ep1")

    def test_invalid_dates_and_failures(self):
        with self.assertRaisesRegex(PipelineError, "YYYY-MM-DD"):
            collect(FakeAnalytics({}), self.store, ["v1"], "2026/10/01", "2026-10-02")
        with self.assertRaisesRegex(PipelineError, "after end"):
            collect(FakeAnalytics({}), self.store, ["v1"], "2026-10-03", "2026-10-02")
        with self.assertRaisesRegex(PipelineError, "video v1 failed"):
            collect(FakeAnalytics({}, fail=RuntimeError("403")), self.store, ["v1"], "2026-10-01", "2026-10-02")
        with self.assertRaisesRegex(PipelineError, "without video_id"):
            collect(FakeAnalytics({}), self.store, [{"episode": "x"}], "2026-10-01", "2026-10-02")

    def test_missing_columns_raise(self):
        service = FakeAnalytics({"v1": []})
        service.drop = "likes"
        with self.assertRaisesRegex(PipelineError, "lacks columns \\['likes'\\]"):
            collect(service, self.store, ["v1"], "2026-10-01", "2026-10-02")

    def test_summarize_weights_retention_by_views(self):
        rows = [{"video_id": "v1", "date": "d1", "views": 100, "averageViewPercentage": 40.0,
                 "estimatedMinutesWatched": 120, "likes": 2, "subscribersGained": 1},
                {"video_id": "v1", "date": "d2", "views": 300, "averageViewPercentage": 60.0,
                 "estimatedMinutesWatched": 60, "likes": 1, "subscribersGained": 0},
                {"video_id": "v2", "date": "d1", "views": 5}]
        summary = summarize(rows, "v1")
        self.assertEqual(summary["views"], 400)
        self.assertEqual(summary["watch_hours"], 3.0)
        self.assertEqual(summary["retention"], 0.55)
        self.assertIsNone(summary["ctr"])
        self.assertIsNone(summarize([], "v9")["retention"])

    def test_table_store_upserts_by_partition_and_row(self):
        table = FakeTable()
        store = TableAnalyticsStore(table)
        collect(FakeAnalytics({"v1": [row("2026-10-01", 1, 10.0)]}), store, ["v1"], "2026-10-01", "2026-10-01")
        collect(FakeAnalytics({"v1": [row("2026-10-01", 7, 10.0)]}), store, ["v1"], "2026-10-01", "2026-10-01")
        self.assertEqual(list(table.entities), [("v1", "2026-10-01")])
        self.assertEqual(store.rows("v1")[0]["views"], 7)
        self.assertNotIn("PartitionKey", store.rows()[0])


if __name__ == "__main__":
    unittest.main()
