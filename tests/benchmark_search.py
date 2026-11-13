"""Offline 20,000-entry search check: python tests/benchmark_search.py."""
import json
import math
import platform
import statistics
import tempfile
import time
from pathlib import Path

from recall import RecallTools, catalog
from recall.db import init_db, open_readwrite
from recall.urls import sanitize_url


def main():
    measurements = []
    with tempfile.TemporaryDirectory() as directory:
        path = init_db(Path(directory) / "bench.db")
        conn = open_readwrite(path)
        try:
            catalog.ensure_source(conn, "bench", "manual", "web")
            catalog.merge_entries(conn, "bench", (
                catalog.EntryInput(sanitize_url(f"https://example.com/{i}"),
                    title=f"日本电商研究 {i}" if i % 10 == 0 else f"资料 {i}")
                for i in range(20_000)
            ))
        finally:
            conn.close()
        with RecallTools.open(path) as tools:
            for query in ("日本", "资料", None):
                first = tools.recall_search(query)
                second = tools.recall_search(query, cursor=first["next_cursor"])
                assert len(first["items"]) == len(second["items"]) == 20
                assert not ({i["item_id"] for i in first["items"]} & {i["item_id"] for i in second["items"]})
                samples = []
                for _ in range(20):
                    started = time.perf_counter()
                    tools.recall_search(query)
                    samples.append((time.perf_counter() - started) * 1000)
                measurements.append({"query": query, "samples": len(samples),
                    "p95_ms": round(sorted(samples)[math.ceil(len(samples) * .95) - 1], 1),
                    "median_ms": round(statistics.median(samples), 1)})
    print(json.dumps({"entries": 20_000, "python": platform.python_version(),
                      "cpu": platform.processor(), "queries": measurements}, indent=2))


if __name__ == "__main__":
    main()
