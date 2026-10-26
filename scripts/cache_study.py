#!/usr/bin/env python3
"""Measure the whole-run cache on the Compose API.

Each synthetic case is submitted twice: once as a miss and once as a repeat that
should hit the cache.  The script records wall time, cached flag, and the
returned usage object.  It writes a JSON artifact suitable for
``reports/cache_study.json``.

This is a measurement helper, not a load test.  It uses synthetic data only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import httpx


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower),
        2,
    )


def sum_usage(rows: list[dict], key: str) -> float:
    return sum((row["usage"] or {}).get(key, 0) for row in rows)


def sum_cost(rows: list[dict]) -> float:
    return sum((row["usage"] or {}).get("cost_usd", 0) for row in rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cases", type=int, default=10)
    parser.add_argument("--client-id", default="cache-study")
    parser.add_argument("--output", default="reports/cache_study.json")
    args = parser.parse_args()

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    client = httpx.Client(
        base_url=args.base_url,
        timeout=60,
        headers={"X-Client-Id": args.client_id},
    )
    records: list[dict] = []

    def submit(payload: dict) -> str:
        response = client.post("/v1/advisories", json=payload)
        response.raise_for_status()
        return response.json()["run_id"]

    def poll(run_id: str) -> dict:
        while True:
            response = client.get(f"/v1/advisories/{run_id}")
            response.raise_for_status()
            body = response.json()
            if body["status"] in {"completed", "escalated", "failed"}:
                return body
            time.sleep(0.05)

    for case_index in range(args.cases):
        payload = {
            "question": "An employee resigned after 5 years. How is gratuity calculated?",
            "establishment": {
                "establishment_type": "it_services",
                "employee_count": 120,
            },
            "employment": {
                "worker_type": "permanent",
                "event": "resignation",
                "completed_service_months": 60 + case_index,
                "monthly_wage_inr": 40000 + case_index * 1000,
            },
            "requester": {"client_id": args.client_id, "role": "hr"},
            "consent": True,
            "notes": f"cache-study-{stamp}-{case_index}",
        }
        for repeat in ("miss", "hit"):
            started = time.perf_counter()
            run_id = submit(payload)
            body = poll(run_id)
            wall_ms = (time.perf_counter() - started) * 1000
            result = body.get("result") or {}
            records.append(
                {
                    "case": case_index,
                    "repeat": repeat,
                    "run_id": run_id,
                    "status": body["status"],
                    "cached": result.get("cached"),
                    "wall_ms": round(wall_ms, 2),
                    "usage": result.get("usage"),
                }
            )

    misses = [row for row in records if row["repeat"] == "miss"]
    hits = [row for row in records if row["repeat"] == "hit"]
    summary = {
        "generated_at": stamp,
        "base_url": args.base_url,
        "requests": len(records),
        "miss": {
            "n": len(misses),
            "p50_wall_ms": percentile([row["wall_ms"] for row in misses], 0.5),
            "p95_wall_ms": percentile([row["wall_ms"] for row in misses], 0.95),
            "prompt_tokens": sum_usage(misses, "prompt_tokens"),
            "completion_tokens": sum_usage(misses, "completion_tokens"),
            "llm_calls": sum_usage(misses, "llm_calls"),
            "cost_usd_total": round(sum_cost(misses), 8),
            "cost_usd_per_run": round(sum_cost(misses) / len(misses), 8)
            if misses
            else None,
            "cost_usd_per_100_runs": round(sum_cost(misses) / len(misses) * 100, 6)
            if misses
            else None,
        },
        "hit": {
            "n": len(hits),
            "p50_wall_ms": percentile([row["wall_ms"] for row in hits], 0.5),
            "p95_wall_ms": percentile([row["wall_ms"] for row in hits], 0.95),
            "prompt_tokens_returned": sum_usage(hits, "prompt_tokens"),
            "completion_tokens_returned": sum_usage(hits, "completion_tokens"),
            "llm_calls_incremental": 0,
            "cost_usd_incremental": 0.0,
            "note": (
                "Cached result echoes the original usage/cost metadata; cache hits "
                "make no provider calls."
            ),
        },
        "records": records,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}, indent=2))


if __name__ == "__main__":
    main()
