"""Rough single-process throughput benchmark (in-process service, file-backed SQLite in WAL mode).

python scripts/benchmark.py [N]
"""

from __future__ import annotations

import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_platform.persistence.db import Database  # noqa: E402
from decision_platform.service import DecisionService  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 500
svc = DecisionService(Database(Path(tempfile.mkdtemp()) / "bench.db"), sleep=lambda s: None, rng=random.Random(1))
svc.load_workflows(ROOT / "config" / "workflows")
lat = []
t0 = time.perf_counter()
for i in range(N):
    p = {
        "applicant": {
            "id": f"APP-B{i}",
            "name": "Bench",
            "age": 30 + i % 30,
            "email": f"b{i}@example.com",
            "monthly_income": 100000 + i,
            "monthly_debt": 5000,
            "employment_status": "employed",
        },
        "loan": {"amount": 300000 + i, "term_months": 48, "purpose": "personal"},
    }
    s = time.perf_counter()
    svc.submit("loan_application", p, f"bench-{i}")
    lat.append((time.perf_counter() - s) * 1000)
total = time.perf_counter() - t0
lat.sort()
events = svc.db.read().execute("SELECT COUNT(*) c FROM audit_events").fetchone()["c"]
t1 = time.perf_counter()
ok = svc.verify_audit()["ok"]
verify_s = time.perf_counter() - t1
print(
    f"requests={N}  throughput={N / total:.0f} req/s  p50={statistics.median(lat):.1f}ms  "
    f"p95={lat[int(N * 0.95) - 1]:.1f}ms  p99={lat[int(N * 0.99) - 1]:.1f}ms  "
    f"audit_events={events} ({events / N:.1f}/request)  verify_chain={verify_s * 1000:.0f}ms ok={ok}"
)
