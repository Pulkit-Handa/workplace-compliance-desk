"""Bounded, observable tools used by the advisory agents.

Tools are the only way an agent may read the corpus.  Each call is subject to a
wall-clock timeout and counted against the run's tool budget.  A tool never
reaches the network, so the retrieval path is reproducible and inspectable.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Generic, TypeVar

from compliance.config import settings
from compliance.corpus.index import IndexStore, SearchHit, get_default_index
from compliance.corpus.models import Chunk
from compliance.observability import metrics
from compliance.util import parse_date

T = TypeVar("T")


class ToolBudgetExceeded(RuntimeError):
    """Raised when an agent attempts to exceed MAX_TOOL_CALLS."""


@dataclass
class ToolResult(Generic[T]):
    tool: str
    ok: bool
    data: T | None
    latency_ms: float
    error: str | None = None
    cached: bool = False

    def as_trace(self) -> dict[str, Any]:
        trace: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "cached": self.cached,
            "error": self.error,
            "items": len(self.data) if isinstance(self.data, list) else (1 if self.data else 0),
        }
        passages = self._passage_trace()
        if passages:
            trace["passages"] = passages
        return trace

    def _passage_trace(self) -> list[dict[str, Any]]:
        """Record the retrieved chunks, not just their count.

        A run must be reconstructible from its trace.  Search hits and direct
        lookup/related results carry the chunk id, citation, status, score and
        a bounded preview so a reviewer can see exactly what the agent read.
        """
        if not isinstance(self.data, list):
            return []
        passages: list[dict[str, Any]] = []
        for item in self.data:
            if isinstance(item, SearchHit):
                chunk, score = item.chunk, round(item.score, 4)
            elif isinstance(item, Chunk):
                chunk, score = item, None
            else:
                continue
            passages.append(
                {
                    "id": chunk.id,
                    "provision_id": chunk.provision_id,
                    "citation": chunk.citation,
                    "status": chunk.status,
                    "score": score,
                    "preview": (chunk.text or "")[:240],
                }
            )
        return passages


@dataclass
class ProvisionStatus:
    provision_id: str
    citation: str
    status: str
    in_force_on: bool
    effective_from: date | None
    effective_to: date | None
    repealed_by: str | None
    successor: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provision_id": self.provision_id,
            "citation": self.citation,
            "status": self.status,
            "in_force_on": self.in_force_on,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "repealed_by": self.repealed_by,
            "successor": self.successor,
            "reason": self.reason,
        }


@dataclass
class Coverage:
    domain: str
    covered: bool | None
    threshold: int | None
    employee_count: int | None
    provision_id: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "covered": self.covered,
            "threshold": self.threshold,
            "employee_count": self.employee_count,
            "provision_id": self.provision_id,
            "reason": self.reason,
        }


_DOCUMENT_TITLES = {
    "ssc2020": "Code on Social Security, 2020",
    "gratuity1972": "Payment of Gratuity Act, 1972",
    "posh2013": "POSH Act, 2013",
    "commencement2025": "Labour Codes Commencement Notification, 2025",
}

# The Code on Social Security commenced on this date.  Before it, the 1972 Act
# governs gratuity.  Keeping the date here (and not hard-coded in an agent)
# makes historical `as_of_date` questions answerable and auditable.
SSC_COMMENCEMENT = date(2025, 11, 21)


@dataclass
class ToolBox:
    """A per-run tool handle.

    One instance per run means budgets and caches cannot leak between callers.
    """

    index: IndexStore | None = None
    as_of: date = field(default_factory=date.today)
    max_calls: int | None = None
    timeout_s: float | None = None
    calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)
    _cache: dict[str, ToolResult[Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.max_calls = self.max_calls or settings.max_tool_calls
        self.timeout_s = self.timeout_s or settings.tool_timeout_s

    @property
    def store(self) -> IndexStore:
        if self.index is None:
            self.index = get_default_index()
        return self.index

    def remaining(self) -> int:
        return max(0, self.max_calls - self.calls)

    async def _run(
        self,
        tool: str,
        fn: Callable[[], T],
        *,
        cache_key: str | None = None,
    ) -> ToolResult[T]:
        if cache_key and cache_key in self._cache:
            cached = self._cache[cache_key]
            metrics.TOOL_CALLS.labels(tool=tool, outcome="cache_hit").inc()
            return ToolResult(
                tool=cached.tool,
                ok=cached.ok,
                data=cached.data,
                latency_ms=0.0,
                error=cached.error,
                cached=True,
            )
        if self.calls >= self.max_calls:
            metrics.TOOL_CALLS.labels(tool=tool, outcome="budget_exceeded").inc()
            raise ToolBudgetExceeded(f"tool budget of {self.max_calls} exhausted")
        self.calls += 1
        started = time.perf_counter()
        try:
            data = await asyncio.wait_for(asyncio.to_thread(fn), timeout=self.timeout_s)
            latency = (time.perf_counter() - started) * 1000
            result: ToolResult[T] = ToolResult(tool, True, data, latency)
            metrics.TOOL_CALLS.labels(tool=tool, outcome="ok").inc()
        except TimeoutError:
            latency = (time.perf_counter() - started) * 1000
            result = ToolResult(tool, False, None, latency, error="timeout")
            metrics.TOOL_CALLS.labels(tool=tool, outcome="timeout").inc()
        except Exception as exc:  # a tool failure must not crash the run
            latency = (time.perf_counter() - started) * 1000
            result = ToolResult(tool, False, None, latency, error=f"{type(exc).__name__}: {exc}"[:300])
            metrics.TOOL_CALLS.labels(tool=tool, outcome="error").inc()
        metrics.TOOL_DURATION.labels(tool=tool).observe(latency / 1000)
        self.log.append(result.as_trace())
        if cache_key and result.ok:
            self._cache[cache_key] = result
        return result

    async def search(
        self,
        query: str,
        *,
        top_k: int = 6,
        in_force_only: bool = False,
        include_repealed: bool = True,
        domain: str | None = None,
    ) -> ToolResult[list[SearchHit]]:
        key = f"search|{query.strip().lower()}|{top_k}|{in_force_only}|{include_repealed}|{domain}"
        return await self._run(
            "search_provisions",
            lambda: self.store.search(
                query,
                top_k=top_k,
                in_force_only=in_force_only,
                include_repealed=include_repealed,
                domain=domain,
            ),
            cache_key=key,
        )

    async def lookup(self, clause: str) -> ToolResult[list[Chunk]]:
        return await self._run(
            "lookup_clause",
            lambda: self.store.lookup_clause(clause),
            cache_key=f"lookup|{clause.strip().lower()}",
        )

    async def related(self, provision_ids: list[str]) -> ToolResult[list[Chunk]]:
        key = "related|" + ",".join(sorted(provision_ids))
        return await self._run(
            "related_provisions",
            lambda: self.store.related_chunks(provision_ids),
            cache_key=key,
        )

    async def currency(self, provision_ids: list[str]) -> ToolResult[list[ProvisionStatus]]:
        def check() -> list[ProvisionStatus]:
            statuses: list[ProvisionStatus] = []
            seen: set[str] = set()
            for provision_id in provision_ids:
                for chunk in self.store.get_provision_chunks(provision_id):
                    if chunk.provision_id in seen:
                        continue
                    seen.add(chunk.provision_id)
                    statuses.append(self._status(chunk))
            return statuses

        return await self._run(
            "check_currency",
            check,
            cache_key="currency|" + ",".join(sorted(provision_ids)) + f"|{self.as_of}",
        )

    def _status(self, chunk: Chunk) -> ProvisionStatus:
        start = parse_date(chunk.effective_from)
        end = parse_date(chunk.effective_to)
        in_force = start is not None and start <= self.as_of and (end is None or self.as_of <= end)
        if chunk.status == "repealed" and in_force:
            reason = (
                f"repealed with effect from {end.isoformat()} by {chunk.repealed_by}; "
                f"it was in force on {self.as_of.isoformat()}"
            )
        elif chunk.status == "repealed":
            reason = (
                f"repealed with effect from {end.isoformat() if end else 'unknown'} by "
                f"{chunk.repealed_by}; not in force on {self.as_of.isoformat()}"
            )
        elif in_force:
            reason = f"in force from {start.isoformat() if start else 'unknown'}"
        else:
            reason = f"not yet in force on {self.as_of.isoformat()}"
        return ProvisionStatus(
            provision_id=chunk.provision_id,
            citation=chunk.citation,
            status=chunk.status,
            in_force_on=in_force,
            effective_from=start,
            effective_to=end,
            repealed_by=chunk.repealed_by,
            successor=_DOCUMENT_TITLES.get(chunk.repealed_by or ""),
            reason=reason,
        )

    async def coverage(
        self,
        *,
        domain: str,
        establishment_type: str,
        employee_count: int | None,
    ) -> ToolResult[Coverage]:
        def compute() -> Coverage:
            if domain == "workplace_harassment":
                return Coverage(
                    domain=domain,
                    covered=True if employee_count is None or employee_count >= 1 else False,
                    threshold=None,
                    employee_count=employee_count,
                    provision_id="posh2013-s4",
                    reason=(
                        "POSH Act, 2013 s. 4 requires every employer of a workplace to constitute an "
                        "Internal Complaints Committee; no employee-count threshold appears in that section."
                    ),
                )
            if domain == "gratuity":
                provision_id = (
                    "ssc2020-sfirst-schedule-chapter-v"
                    if self.as_of >= SSC_COMMENCEMENT
                    else "gratuity1972-s1"
                )
                if establishment_type == "factory":
                    return Coverage(
                        domain, True, 10, employee_count, provision_id,
                        "Chapter V applies to every factory irrespective of the employee count.",
                    )
                if employee_count is None:
                    return Coverage(
                        domain, None, 10, None, provision_id,
                        "A shop or establishment is covered at ten or more employees; the count was not supplied.",
                    )
                return Coverage(
                    domain, employee_count >= 10, 10, employee_count, provision_id,
                    (
                        "Chapter V applies to a shop or establishment with ten or more employees on any day "
                        "of the preceding twelve months."
                    ),
                )
            if domain == "maternity_benefit":
                provision_id = (
                    "ssc2020-sfirst-schedule-chapter-vi"
                    if self.as_of >= SSC_COMMENCEMENT
                    else "ssc2020-sfirst-schedule-chapter-vi"
                )
                if establishment_type == "factory":
                    return Coverage(
                        domain, True, 10, employee_count, provision_id,
                        "Chapter VI applies to every factory irrespective of the employee count.",
                    )
                if employee_count is None:
                    return Coverage(
                        domain, None, 10, None, provision_id,
                        "A shop or establishment is covered at ten or more employees; the count was not supplied.",
                    )
                return Coverage(
                    domain, employee_count >= 10, 10, employee_count, provision_id,
                    "Chapter VI applies to a shop or establishment with ten or more employees.",
                )
            return Coverage(domain, None, None, employee_count, None, "No coverage rule for this domain.")

        return await self._run(
            "check_coverage",
            compute,
            cache_key=f"coverage|{domain}|{establishment_type}|{employee_count}",
        )


__all__ = [
    "Coverage",
    "ProvisionStatus",
    "SSC_COMMENCEMENT",
    "ToolBox",
    "ToolBudgetExceeded",
    "ToolResult",
]
