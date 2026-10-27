"""Locust load profile for the Workplace Compliance Advisory Desk.

Two measurements are recorded:

* the ordinary HTTP calls (POST and each poll) with their own latencies;
* one synthetic ``ADVISORY / e2e`` row per completed run, measured from submit
  to terminal status.  That is the measurement compared with the declared SLO.

Every request carries a unique ``notes`` value so the whole-run cache cannot
turn the load test into a cache benchmark.  Each simulated user also gets its
own client id, so the per-client token bucket is exercised rather than bypassed.
"""
from __future__ import annotations

import time
import uuid

from locust import HttpUser, between, task

TERMINAL = {"completed", "escalated", "failed"}
POLL_INTERVAL_S = 0.1
RUN_TIMEOUT_S = 45.0


def _payload(unique_id: str) -> dict:
    return {
        "establishment": {"establishment_type": "it_services", "employee_count": 120},
        "requester": {"client_id": "load", "role": "hr"},
        "consent": True,
        "question": (
            "A fixed term employee's contract expires after 3 years. Are we liable for "
            "gratuity under the Payment of Gratuity Act 1972 or the Code on Social Security, 2020?"
        ),
        "notes": f"synthetic load case {unique_id}",
        "employment": {
            "worker_type": "fixed_term",
            "event": "expiry",
            "start_date": "2022-11-01",
            "end_date": "2025-11-01",
            "completed_service_months": 36,
            "monthly_wage_inr": 50000,
        },
    }


class AdvisoryUser(HttpUser):
    wait_time = between(0.2, 0.6)

    def on_start(self) -> None:
        self.client_id = f"load-{uuid.uuid4().hex[:10]}"
        self.headers = {
            "X-Client-Id": self.client_id,
            "X-Prompt-Variant": "v2",
            "Content-Type": "application/json",
        }

    @task
    def advisory_run(self) -> None:
        payload = _payload(uuid.uuid4().hex)
        payload["requester"]["client_id"] = self.client_id
        started = time.perf_counter()
        with self.client.post(
            "/v1/advisories",
            json=payload,
            headers=self.headers,
            catch_response=True,
            name="POST /v1/advisories",
        ) as response:
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "1"))
                response.failure(f"rate limited; retry after {retry_after}s")
                time.sleep(min(retry_after, 2))
                return
            if response.status_code not in (200, 202):
                response.failure(f"unexpected status {response.status_code}: {response.text[:200]}")
                self._record_e2e(started, failed=True)
                return
            accepted = response.json()

        run_id = accepted["run_id"]
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while time.monotonic() < deadline:
            poll = self.client.get(
                f"/v1/advisories/{run_id}",
                headers={"X-Client-Id": self.client_id},
                name="GET /v1/advisories/{run_id}",
            )
            if poll.status_code != 200:
                self._record_e2e(started, failed=True)
                return
            body = poll.json()
            if body["status"] in TERMINAL:
                result = body.get("result") or {}
                expected_ok = body["status"] in {"completed", "escalated"}
                self._record_e2e(started, failed=not expected_ok)
                # Surface a broken advisory as a failed request without hiding
                # the latency sample.
                if expected_ok and result.get("verification", {}).get("outcome") != "passed":
                    poll.failure("verification did not pass")
                return
            time.sleep(POLL_INTERVAL_S)
        self._record_e2e(started, failed=True)

    def _record_e2e(self, started: float, *, failed: bool) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if failed:
            self.environment.stats.log_error("ADVISORY", "e2e", "run did not complete successfully")
            return
        self.environment.stats.log_request("ADVISORY", "e2e", elapsed_ms, 0)
