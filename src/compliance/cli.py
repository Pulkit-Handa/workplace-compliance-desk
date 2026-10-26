"""Command line entry point.

    compliance index build [--force]
    compliance index info
    compliance ask --file request.json [--variant v2]
    compliance serve --host 0.0.0.0 --port 8000
    compliance worker
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from compliance.config import settings
from compliance.corpus.index import build_index, get_default_index


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def cmd_index_build(args: argparse.Namespace) -> int:
    result = build_index(force=args.force)
    _print(
        {
            "index_version": result.index_version,
            "reused": result.reused,
            "chunk_count": result.chunk_count,
            "embedded": result.embedded,
            "cache_hits": result.cache_hits,
            "duration_s": round(result.duration_s, 3),
        }
    )
    return 0


def cmd_index_info(args: argparse.Namespace) -> int:
    _print(get_default_index().metadata())
    return 0


def _request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        return json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not args.question:
        raise SystemExit("provide --file or --question")
    return {
        "question": args.question,
        "establishment": {
            "establishment_type": args.establishment_type,
            "employee_count": args.employee_count,
        },
        "employment": {
            "worker_type": args.worker_type,
            "event": args.event,
            "completed_service_months": args.months,
            "monthly_wage_inr": args.wage,
        },
        "requester": {"client_id": args.client_id, "role": "hr"},
        "consent": True,
        "as_of_date": args.as_of,
    }


def cmd_ask(args: argparse.Namespace) -> int:
    from compliance.agents.graph import run_advisory_sync
    from compliance.contracts import ComplianceRequest

    try:
        request = ComplianceRequest.model_validate(_request_from_args(args))
    except Exception as exc:
        print(f"invalid request: {exc}", file=sys.stderr)
        return 2
    result = run_advisory_sync(request, variant=args.variant)
    payload = result.model_dump(mode="json")
    if args.markdown:
        print(result.advisory)
        print()
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "route": result.route.value,
                    "verification": result.verification.outcome.value,
                    "issues": [issue.reason for issue in result.verification.issues],
                    "latency_ms": result.latency_ms,
                },
                indent=2,
            )
        )
    else:
        _print(payload)
    return 0 if result.route.value != "legal_counsel_review" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "compliance.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from compliance.service import worker as worker_module

    asyncio.run(worker_module.main())
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from evals.run_evals import main as eval_main

    return eval_main(["--variant", args.variant, "--out", args.out])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compliance", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="build or inspect the retrieval index")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    build = index_sub.add_parser("build")
    build.add_argument("--force", action="store_true")
    build.set_defaults(func=cmd_index_build)
    info = index_sub.add_parser("info")
    info.set_defaults(func=cmd_index_info)

    ask = sub.add_parser("ask", help="run one advisory in-process")
    ask.add_argument("--file", help="path to a JSON request")
    ask.add_argument("--question")
    ask.add_argument("--client-id", default="cli")
    ask.add_argument("--variant", choices=["v1", "v2"], default=settings.prompt_variant)
    ask.add_argument("--establishment-type", default="it_services")
    ask.add_argument("--employee-count", type=int)
    ask.add_argument("--worker-type", default="permanent")
    ask.add_argument("--event", default="none")
    ask.add_argument("--months", type=int)
    ask.add_argument("--wage", type=float)
    ask.add_argument("--as-of", default=None)
    ask.add_argument("--markdown", action="store_true")
    ask.set_defaults(func=cmd_ask)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=settings.api_port)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="consume runs from Redis Streams")
    worker.set_defaults(func=cmd_worker)

    evaluate = sub.add_parser("eval", help="run the golden-set evaluation")
    evaluate.add_argument("--variant", choices=["v1", "v2"], default="v2")
    evaluate.add_argument("--out", default="reports/eval_report.json")
    evaluate.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
