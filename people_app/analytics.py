"""ClickHouse analytics for PTO and overtime dashboards.

Only synthetic workforce metrics are uploaded: employee id, department, manager id, month, PTO
and overtime hours. Names, contact details, compensation, succession and investigation data are
never read here. Authorization stays in PeopleService: this module only ever receives the list of
employee ids an identity is already allowed to see, and filters on them in the ClickHouse query.

    python -m people_app.analytics upload    # idempotent: re-running adds no duplicates
    python -m people_app.analytics count
"""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from people_app.clickhouse_db import get_client

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data"
DATABASE = "people_analytics"
TABLE = f"{DATABASE}.workforce_monthly"
METRICS = ("pto_opening_hours", "pto_accrued_hours", "pto_used_hours", "pto_closing_hours", "overtime_hours")
COLUMNS = ("employee_id", "department", "manager_id", "month") + METRICS

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    employee_id String,
    department String,
    manager_id String,
    month String,
    pto_opening_hours Int32,
    pto_accrued_hours Int32,
    pto_used_hours Int32,
    pto_closing_hours Int32,
    overtime_hours Int32,
    loaded_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY (employee_id, month)
"""

SOURCE_CLICKHOUSE = "ClickHouse"
SOURCE_FALLBACK = "local fallback (ClickHouse unavailable)"


def source_rows(seed_dir: Path = SEED_DIR) -> list[dict]:
    """Build upload rows from the seed files. Only the allow-listed columns are copied."""
    employees = {e["employee_id"]: e for e in json.loads((seed_dir / "employees.json").read_text())}
    workforce = json.loads((seed_dir / "workforce_monthly.json").read_text())
    rows = []
    for w in workforce:
        emp = employees[w["employee_id"]]
        rows.append({"employee_id": w["employee_id"], "department": emp["department"],
                     "manager_id": emp["manager_id"], "month": w["month"],
                     **{m: int(w[m]) for m in METRICS}})
    return rows


def count_rows(client) -> int:
    # FINAL collapses any rows that share (employee_id, month) but have not been merged yet.
    return int(client.command(f"SELECT count() FROM {TABLE} FINAL"))


def upload(client, rows: list[dict] | None = None) -> dict:
    """Create the table if needed and insert only (employee_id, month) keys not already present.
    The ReplacingMergeTree key is a second guard against duplicates."""
    rows = source_rows() if rows is None else rows
    client.command(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    client.command(SCHEMA)
    existing = {(r[0], r[1]) for r in client.query(f"SELECT employee_id, month FROM {TABLE} FINAL").result_rows}
    new = [r for r in rows if (r["employee_id"], r["month"]) not in existing]
    if new:
        now = datetime.now(timezone.utc)
        client.insert(TABLE, [[r[c] for c in COLUMNS] + [now] for r in new],
                      column_names=list(COLUMNS) + ["loaded_at"])
    return {"source_rows": len(rows), "inserted": len(new), "table_rows": count_rows(client)}


class ClickHouseWorkforce:
    """Reads workforce metrics for an already-authorized set of employee ids.

    The cache key is the identity plus its exact permitted id set, so one identity can never be
    served another's cached rows, and a change in permissions is a cache miss."""

    def __init__(self, client_factory=get_client, ttl_seconds: float = 60, retry_after_seconds: float = 30):
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._ttl = ttl_seconds
        self._retry_after = retry_after_seconds
        self._failed_at = 0.0

    def fetch(self, user_id: str, role: str, employee_ids: list[str]) -> list[dict]:
        allowed = tuple(sorted(set(employee_ids)))
        if not allowed:
            return []
        key = (user_id, role, allowed)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return [dict(r) for r in hit[1]]
            if time.monotonic() - self._failed_at < self._retry_after:
                raise ConnectionError("ClickHouse recently unavailable")
            try:
                if self._client is None:
                    self._client = self._client_factory()
                result = self._client.query(
                    f"SELECT employee_id, month, {', '.join(METRICS)} FROM {TABLE} FINAL "
                    "WHERE has({ids:Array(String)}, employee_id) ORDER BY employee_id, month",
                    parameters={"ids": list(allowed)})
            except Exception:
                self._failed_at = time.monotonic()
                self._client = None
                raise
            rows = [dict(zip(result.column_names, r)) for r in result.result_rows]
            # Defence in depth: never return a row outside the permitted set, even if the query changes.
            rows = [r for r in rows if r["employee_id"] in allowed]
            self._cache[key] = (time.monotonic(), rows)
            return [dict(r) for r in rows]

    def clear_cache(self):
        with self._lock:
            self._cache.clear()


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "count"
    # The driver logs full tracebacks (including the server URL) on HTTP errors; keep output to our summary.
    logging.getLogger("clickhouse_connect").setLevel(logging.CRITICAL)
    try:
        client = get_client(timeout=60)  # Cloud services can take ~30s to wake from idle
    except Exception as e:
        print(f"FAILED: connect ({type(e).__name__})")
        return 1
    try:
        if cmd == "upload":
            print("upload:", upload(client))
        else:
            print("rows in", TABLE, "=", count_rows(client))
    except Exception as e:  # report the type only; driver messages can include connection details
        print(f"FAILED: {cmd} ({type(e).__name__})")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
