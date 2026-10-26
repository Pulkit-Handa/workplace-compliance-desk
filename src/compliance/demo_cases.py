"""Synthetic demo requests.  No real personal data is used anywhere in the repo."""
from __future__ import annotations

DEMO_CASES: list[dict] = [
    {
        "id": "fixed-term-expiry",
        "title": "Fixed-term contract expires after 3 years",
        "obligation": "Gratuity / repealed Act",
        "request": {
            "question": (
                "A fixed term employee's contract expires after 3 years. Are we liable for gratuity "
                "under the Payment of Gratuity Act 1972 or the new Code?"
            ),
            "establishment": {"establishment_type": "it_services", "employee_count": 120},
            "employment": {
                "worker_type": "fixed_term",
                "event": "expiry",
                "start_date": "2022-11-01",
                "end_date": "2025-11-01",
                "completed_service_months": 36,
                "monthly_wage_inr": 50000,
            },
            "requester": {"client_id": "demo-hr", "role": "hr", "organisation": "Demo Technologies"},
            "consent": True,
        },
    },
    {
        "id": "resignation-below-five-years",
        "title": "Resignation after 3 years",
        "obligation": "Gratuity threshold",
        "request": {
            "question": "An employee resigned after 3 years. Is any gratuity payable?",
            "establishment": {"establishment_type": "it_services", "employee_count": 120},
            "employment": {
                "worker_type": "permanent",
                "event": "resignation",
                "completed_service_months": 36,
                "monthly_wage_inr": 45000,
            },
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "death-in-service",
        "title": "Death in service after 2 years",
        "obligation": "Gratuity / five-year waiver",
        "request": {
            "question": (
                "An employee died in service after 2 years. Is gratuity payable to the nominee despite "
                "the five year rule?"
            ),
            "establishment": {"establishment_type": "factory", "employee_count": 80},
            "employment": {
                "worker_type": "permanent",
                "event": "death",
                "completed_service_months": 24,
                "monthly_wage_inr": 40000,
            },
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "small-establishment",
        "title": "Seven-person startup",
        "obligation": "Coverage threshold",
        "request": {
            "question": "We are a 7-person startup. Does gratuity apply to us when an employee resigns?",
            "establishment": {"establishment_type": "shop", "employee_count": 7},
            "employment": {
                "worker_type": "permanent",
                "event": "resignation",
                "completed_service_months": 72,
                "monthly_wage_inr": 55000,
            },
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "maternity-two-children",
        "title": "Maternity benefit with two children",
        "obligation": "Maternity benefit",
        "request": {
            "question": (
                "A pregnant employee has worked 100 days in the last 12 months and already has two "
                "children. What maternity benefit is she entitled to?"
            ),
            "establishment": {"establishment_type": "it_services", "employee_count": 120},
            "employment": {
                "worker_type": "permanent",
                "event": "none",
                "is_pregnant": True,
                "service_days_last_12_months": 100,
                "children_count": 2,
            },
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "posh-policy",
        "title": "POSH committee and complaint timeline",
        "obligation": "POSH Act",
        "request": {
            "question": (
                "What are our obligations to constitute an Internal Complaints Committee and what is "
                "the complaint timeline under the POSH Act?"
            ),
            "establishment": {
                "establishment_type": "it_services",
                "employee_count": 200,
                "has_internal_committee": False,
            },
            "employment": {"worker_type": "permanent", "event": "none"},
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "posh-complaint-escalation",
        "title": "Specific harassment complaint",
        "obligation": "Escalation boundary",
        "request": {
            "question": "An employee has alleged sexual harassment by her manager. Should we terminate the manager?",
            "establishment": {"establishment_type": "it_services", "employee_count": 200},
            "employment": {"worker_type": "permanent", "event": "none"},
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
    {
        "id": "historical-1972",
        "title": "Gratuity as at 1 June 2024",
        "obligation": "Historical regime",
        "request": {
            "question": "As of 1 June 2024, what gratuity was payable to an employee who resigned after 6 years?",
            "as_of_date": "2024-06-01",
            "establishment": {"establishment_type": "it_services", "employee_count": 120},
            "employment": {
                "worker_type": "permanent",
                "event": "resignation",
                "completed_service_months": 72,
                "monthly_wage_inr": 50000,
            },
            "requester": {"client_id": "demo-hr", "role": "hr"},
            "consent": True,
        },
    },
]


__all__ = ["DEMO_CASES"]
