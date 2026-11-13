from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any

import pytest

from decision_platform.domain.clock import FixedClock
from decision_platform.persistence.db import Database
from decision_platform.service import DecisionService

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "config" / "workflows"
CANDIDATES = ROOT / "config" / "candidates"


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def svc(db: Database, clock: FixedClock, sleeps: SleepRecorder) -> DecisionService:
    service = DecisionService(db, clock=clock, sleep=sleeps, rng=random.Random(1234))
    service.load_workflows(WORKFLOWS)
    return service


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


LOAN_BASE = {
    "applicant": {
        "id": "APP-1001",
        "name": "Asha Rao",
        "age": 34,
        "email": "asha@example.com",
        "monthly_income": 150000,
        "monthly_debt": 10000,
        "employment_status": "employed",
        "country": "IN",
    },
    "loan": {"amount": 600000, "term_months": 60, "purpose": "auto"},
}


@pytest.fixture
def loan():
    def make(**override: Any) -> dict[str, Any]:
        return deep_merge(LOAN_BASE, override)

    return make


CLAIM_BASE = {
    "claim_id": "CLM-2026-0001",
    "policy_number": "POL-500",
    "claimant": {"id": "CUS-9", "name": "Ravi Mehta"},
    "incident": {"date": "2026-11-01", "type": "collision", "description": "Rear-ended at a traffic signal"},
    "amount_claimed": 42000,
    "documents": [{"type": "repair_estimate", "number": "EST-7781"}, {"type": "photo", "number": "IMG-2231"}],
}


@pytest.fixture
def claim():
    def make(**override: Any) -> dict[str, Any]:
        return deep_merge(CLAIM_BASE, override)

    return make


ONBOARDING_BASE = {
    "candidate": {"id": "CAND-501", "name": "Meera Iyer", "email": "meera@example.com", "country": "IN"},
    "role": {"title": "Backend Engineer", "department": "Platform", "level": "L3", "start_date": "2026-12-01"},
    "documents": [{"type": "passport", "number": "P1234567"}, {"type": "degree_certificate", "number": "DEG-88"}],
    "contract_signed": True,
    "requested_access": ["email", "code_repo"],
}

VENDOR_BASE = {
    "requested_by": "procurement.lead",
    "vendor": {
        "name": "Acme Facility Services",
        "country": "IN",
        "tax_id": "24AAACA1234F1Z5",
        "category": "facilities",
        "annual_contract_value": 300000,
        "bank_account_verified": True,
    },
}

KYC_BASE = {
    "subject_id": "SUB-42",
    "documents": [
        {"type": "passport", "number": "Z1234567", "expiry_date": "2030-05-01", "issuing_country": "IN"},
        {"type": "utility_bill", "number": "UB-9981", "expiry_date": "2027-02-01", "issuing_country": "IN"},
    ],
}


def statuses(svc: DecisionService, request_id: str) -> list[str]:
    return [t["to_status"] for t in svc.history(request_id)["transitions"]]
