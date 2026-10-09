"""Backend for employee and manager self-service.

Every permission decision happens here. The Streamlit UI only calls these
functions and never reads data files directly, so a bug or trick in the UI
cannot bypass the checks.

Storage:
  - Seed files in data/ and policy/ are read-only and never written.
  - Runtime changes go to runtime/ (contact and leave copies, approvals,
    audit log, change history).

PTO definition used here: approving leave *reserves* hours. Monthly PTO rows
are system history and are never edited; the reservation is computed from
approved requests and becomes "usage" only when the system closes that month
(not built yet). So hours are never deducted twice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
SEED_DIR = ROOT / "data"
POLICY_FILE = ROOT / "policy" / "access_policy.json"
DEFAULT_RUNTIME_DIR = ROOT / "runtime"

EDITABLE_CONTACT_FIELDS = ("address", "phone")
PTO_FIELDS = ("month", "pto_opening_hours", "pto_accrued_hours", "pto_used_hours", "pto_closing_hours")
OVERTIME_FIELDS = ("month", "overtime_hours")
LEAVE_DECISIONS = {"approve": "approved", "deny": "denied"}

PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().\-]{6,23}$")
ADDRESS_RE = re.compile(r"^[\w .,#'/\-]+$")


class AccessDenied(Exception):
    def __init__(self, reason_code: str):
        super().__init__(f"Access denied ({reason_code})")
        self.reason_code = reason_code


class ValidationError(Exception):
    pass


class ConflictError(Exception):
    """Allowed, but the record is no longer in a state where the action applies."""


class AuditFailure(Exception):
    """Raised when the audit log cannot be written. The operation is blocked."""


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    employee_id: str | None
    display_name: str
    scope_ids: tuple[str, ...] = dc_field(default=())


@dataclass
class Ctx:
    """An authorized request; carried through so every log line shares a request_id."""
    request_id: str
    actor: Actor
    action: str
    resource: str
    target_id: str
    fields: list[str]
    scope: str
    reason: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Policy:
    """Default-deny evaluation of policy/access_policy.json."""

    def __init__(self, path: Path):
        data = json.loads(Path(path).read_text())
        if data.get("default") != "deny":
            raise ValueError("Policy must be default-deny")
        self.rules = data["rules"]

    def _matching(self, role: str, action: str, resource: str) -> list[dict]:
        return [r for r in self.rules
                if r["role"] == role and resource in r["resource"].split(",") and action in r["actions"]]

    def has_rule(self, role: str, action: str, resource: str) -> bool:
        return bool(self._matching(role, action, resource))

    def decide(self, actor: Actor, action: str, resource: str,
               in_scope: Callable[[str], bool]) -> tuple[bool, str, str]:
        """Return (allowed, reason_code, scope). in_scope(scope) answers whether
        the target falls inside that scope for this actor."""
        rules = self._matching(actor.role, action, resource)
        if not rules:
            return False, "NO_MATCHING_RULE", "none"
        for rule in rules:
            if in_scope(rule["scope"]):
                return True, f"ALLOWED_{rule['scope'].upper()}", rule["scope"]
        return False, "OUT_OF_SCOPE", rules[0]["scope"]


class PeopleService:
    def __init__(self, seed_dir: Path = SEED_DIR, runtime_dir: Path = DEFAULT_RUNTIME_DIR,
                 policy_file: Path = POLICY_FILE, analytics=None):
        self.seed_dir = Path(seed_dir)
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.policy = Policy(policy_file)
        # Optional ClickHouse reader for PTO/overtime metrics (people_app.analytics.ClickHouseWorkforce).
        # Called only after authorization, with the authorized employee ids. None means local seed data.
        self.analytics = analytics
        self._lock = threading.RLock()

        self._employees = {e["employee_id"]: e for e in self._load_seed("employees.json")}
        self._workforce = self._load_seed("workforce_monthly.json")
        self._actors = self._build_actors()

        self.contacts_file = self.runtime_dir / "contact_details.json"
        self.leave_file = self.runtime_dir / "leave_requests.json"
        self.approvals_file = self.runtime_dir / "approvals.jsonl"
        self.events_file = self.runtime_dir / "access_events.jsonl"
        self.history_file = self.runtime_dir / "change_history.jsonl"
        for runtime, seed in ((self.contacts_file, "contact_details.json"), (self.leave_file, "leave_requests.json")):
            if not runtime.exists():
                self._write_json_atomic(runtime, self._load_seed(seed))

    # ---------- identities ----------

    def _build_actors(self) -> dict[str, Actor]:
        actors: dict[str, Actor] = {}
        for u in self._load_seed("demo_users.json"):
            emp = self._employees.get(u["employee_id"] or "")
            name = emp["display_name"] if emp else u["user_id"]
            actors[u["user_id"]] = Actor(u["user_id"], u["role"], u["employee_id"], name, tuple(u["scope_ids"]))
        # demo_users.json only lists two employees; give every employee a
        # simulated self-service identity using the same naming pattern (E001 -> U_EMP01).
        covered = {a.employee_id for a in actors.values()}
        for eid, emp in self._employees.items():
            if eid not in covered:
                uid = f"U_EMP{int(eid[1:]):02d}"
                actors[uid] = Actor(uid, "employee", eid, emp["display_name"], (eid,))
        return actors

    def demo_identities(self) -> list[Actor]:
        order = {"employee": 0, "manager": 1}
        return sorted((a for a in self._actors.values() if a.role in order),
                      key=lambda a: (order[a.role], a.employee_id or a.user_id))

    def get_actor(self, user_id: str) -> Actor:
        actor = self._actors.get(user_id)
        if actor is None:
            self._log(str(uuid.uuid4()), str(user_id)[:40], "unverified", "login", "identity", "", [],
                      "none", "DENY", "UNKNOWN_IDENTITY", "not_executed")
            raise AccessDenied("UNKNOWN_IDENTITY")
        return actor

    # ---------- scope resolution ----------

    def _team(self, actor: Actor) -> list[str]:
        """Direct reports: listed in the manager's scope AND reporting to them in employees.json."""
        if actor.role != "manager":
            return []
        return [eid for eid in actor.scope_ids
                if self._employees.get(eid, {}).get("manager_id") == actor.user_id]

    def _in_scope(self, actor: Actor, scope: str, target: str) -> bool:
        if scope == "self":
            return actor.employee_id is not None and target == actor.employee_id
        if scope == "direct_team":
            return target in self._team(actor)
        if scope == "direct_team_assigned_request":
            req = self._find_leave(self._read_json(self.leave_file), target)
            return (req is not None and req["assigned_manager_id"] == actor.user_id
                    and req["employee_id"] in self._team(actor)
                    and req["employee_id"] != actor.employee_id)
        if scope == "assigned_audit_scope":
            return target in actor.scope_ids
        return False  # scopes not implemented yet fail closed

    def _check(self, actor: Actor, action: str, resource: str, target: str) -> tuple[bool, str, str]:
        return self.policy.decide(actor, action, resource, lambda s: self._in_scope(actor, s, target))

    # ---------- employee: own records ----------

    def view_contact(self, actor: Actor, target_employee_id: str) -> dict:
        ctx = self._authorize(actor, "view", "contact", target_employee_id, list(EDITABLE_CONTACT_FIELDS))
        row = self._find_contact(self._read_json(self.contacts_file), target_employee_id)
        self._outcome(ctx, "succeeded")
        return {"employee_id": row["employee_id"], "address": row["address"], "phone": row["phone"]}

    def view_pto(self, actor: Actor, target_employee_id: str) -> dict:
        """Monthly PTO plus hours reserved by approved upcoming leave."""
        ctx = self._authorize(actor, "view", "pto", target_employee_id, list(PTO_FIELDS) + ["reserved_hours"])
        data, source = self._workforce_for(actor, [target_employee_id])
        result = self._pto_summary(target_employee_id, data)
        result["source"] = source
        self._outcome(ctx, "succeeded")
        return result

    # ---------- manager: direct team ----------

    def view_overtime(self, actor: Actor, target_employee_id: str) -> list[dict]:
        ctx = self._authorize(actor, "view", "overtime", target_employee_id, list(OVERTIME_FIELDS))
        data, _ = self._workforce_for(actor, [target_employee_id])
        rows = self._rows(target_employee_id, OVERTIME_FIELDS, data)
        self._outcome(ctx, "succeeded")
        return rows

    def view_team_workforce(self, actor: Actor) -> list[dict]:
        return self.view_team_analytics(actor)["rows"]

    def view_team_analytics(self, actor: Actor) -> dict:
        """PTO and overtime for every direct report, one row per employee per month, plus the data
        source. Each member is checked against the policy for both resources before any query."""
        fields = list(PTO_FIELDS) + ["overtime_hours", "reserved_hours"]
        team = self._team(actor)
        if not team:
            self._deny(actor, "view", "pto,overtime", "direct_team", fields, "none",
                       "NO_MATCHING_RULE" if actor.role != "manager" else "NO_DIRECT_TEAM")
        for eid in team:
            for resource in ("pto", "overtime"):
                allowed, reason, scope = self._check(actor, "view", resource, eid)
                if not allowed:
                    self._deny(actor, "view", resource, eid, fields, scope, reason)
        ctx = Ctx(str(uuid.uuid4()), actor, "view", "pto,overtime", f"direct_team({len(team)})",
                  fields, "direct_team", "ALLOWED_DIRECT_TEAM")
        self._log_ctx(ctx, "ALLOW", "authorized")
        data, source = self._workforce_for(actor, team)
        out = []
        for eid in team:
            summary = self._pto_summary(eid, data)
            overtime = {r["month"]: r["overtime_hours"] for r in self._rows(eid, OVERTIME_FIELDS, data)}
            for row in summary["months"]:
                out.append({"employee_id": eid, "display_name": self._employees[eid]["display_name"],
                            **row, "overtime_hours": overtime.get(row["month"]),
                            "reserved_hours": summary["reserved_hours"]})
        self._outcome(ctx, "succeeded")
        return {"rows": out, "source": source}

    def list_leave_requests(self, actor: Actor) -> list[dict]:
        """Pending and decided leave requests this manager is assigned to decide."""
        fields = ["request_id", "employee_id", "start_date", "hours", "status"]
        if not self.policy.has_rule(actor.role, "approve", "leave_request"):
            self._deny(actor, "view", "leave_request", "assigned", fields, "none", "NO_MATCHING_RULE")
        visible = [r for r in self._read_json(self.leave_file)
                   if self._check(actor, "approve", "leave_request", r["request_id"])[0]]
        ctx = Ctx(str(uuid.uuid4()), actor, "view", "leave_request", f"assigned({len(visible)})",
                  fields, "direct_team_assigned_request", "ALLOWED_FOR_DECISION")
        self._log_ctx(ctx, "ALLOW", "authorized")
        self._outcome(ctx, "succeeded")
        return [{**r, "display_name": self._employees[r["employee_id"]]["display_name"]} for r in visible]

    def decide_leave(self, actor: Actor, request_id: str, decision: str) -> dict:
        if decision not in LEAVE_DECISIONS:
            self._deny(actor, str(decision)[:40], "leave_request", request_id, ["status"], "none",
                       "OPERATION_NOT_ALLOWLISTED")
        ctx = self._authorize(actor, decision, "leave_request", request_id, ["status"])

        with self._lock:
            requests = self._read_json(self.leave_file)
            req = self._find_leave(requests, request_id)
            if req["status"] != "pending":
                self._outcome(ctx, "rejected_not_pending")
                raise ConflictError(f"Request {request_id} is already {req['status']}")
            # Audit must succeed before any mutation happens.
            self._outcome(ctx, "started")

            before, after = req["status"], LEAVE_DECISIONS[decision]
            fingerprint = hashlib.sha256(json.dumps(
                {k: req[k] for k in ("request_id", "employee_id", "start_date", "hours")},
                sort_keys=True).encode()).hexdigest()
            approval = {
                "approval_id": str(uuid.uuid4()),
                "request_id": request_id,
                "approver_id": actor.user_id,
                "decision": after,
                "request_fingerprint": fingerprint,
                "timestamp_utc": _now(),
            }
            history = self._history(ctx, req["employee_id"], "leave_request.status", before, after,
                                    approver_id=actor.user_id, approval_id=approval["approval_id"])
            req["status"] = after
            req["decided_by"] = actor.user_id
            self._commit(ctx, self.leave_file, requests,
                         [(self.approvals_file, approval), (self.history_file, history)])
        self._outcome(ctx, "succeeded")
        return {"request_id": request_id, "status": after, "approval_id": approval["approval_id"]}

    # ---------- writes ----------

    def edit_contact(self, actor: Actor, target_employee_id: str, field: str, new_value: str) -> dict:
        if field not in EDITABLE_CONTACT_FIELDS:
            self._deny(actor, "edit", "contact", target_employee_id, [str(field)[:40]], "none", "FIELD_NOT_EDITABLE")
        ctx = self._authorize(actor, "edit", "contact", target_employee_id, [field])

        try:
            value = self._validate(field, new_value)
        except ValidationError:
            self._outcome(ctx, "rejected_invalid_value")
            raise

        # Audit must succeed before any mutation happens.
        self._outcome(ctx, "started")

        with self._lock:
            contacts = self._read_json(self.contacts_file)
            row = self._find_contact(contacts, target_employee_id)
            before = row[field]
            if before == value:
                self._outcome(ctx, "no_change")
                return {"changed": False}
            row[field] = value
            history = self._history(ctx, target_employee_id, field, before, value)
            self._commit(ctx, self.contacts_file, contacts, [(self.history_file, history)])
        self._outcome(ctx, "succeeded")
        return {"changed": True, "change_id": history["change_id"]}

    def edit_pto(self, actor: Actor, target_employee_id: str, field: str, new_value) -> None:
        """PTO is maintained by the system only. Always denied, always logged."""
        self._deny(actor, "edit", "pto", target_employee_id, [str(field)[:40]], "none", "PTO_SYSTEM_MAINTAINED")

    def edit_workforce(self, actor: Actor, target_employee_id: str, field: str, new_value) -> None:
        """Overtime and other workforce rows are system records; no direct edits."""
        self._deny(actor, "edit", "workforce", target_employee_id, [str(field)[:40]], "none", "DIRECT_EDIT_NOT_ALLOWED")

    def share(self, actor: Actor, target: str, resource: str) -> None:
        """Sharing/export is disabled in v1 for every role."""
        self._deny(actor, "share", str(resource)[:40], str(target)[:40], [], "none", "SHARING_DISABLED")

    def perform(self, actor: Actor, action: str, target: str, field: str, value=None):
        """Single entry point mapping (action, field) to an allowlisted operation.
        Anything not on the list is denied and logged."""
        if action == "view" and field == "contact":
            return self.view_contact(actor, target)
        if action == "view" and field == "pto":
            return self.view_pto(actor, target)
        if action == "view" and field == "overtime":
            return self.view_overtime(actor, target)
        if action == "edit" and field in EDITABLE_CONTACT_FIELDS:
            return self.edit_contact(actor, target, field, value)
        if action == "edit" and field.startswith("pto"):
            return self.edit_pto(actor, target, field, value)
        if action == "edit" and field in ("overtime_hours", "workforce"):
            return self.edit_workforce(actor, target, field, value)
        if action in LEAVE_DECISIONS and field == "leave_request":
            return self.decide_leave(actor, target, action)
        if action in ("share", "export"):
            return self.share(actor, target, field)
        self._deny(actor, str(action)[:40], str(field)[:40], str(target)[:40], [str(field)[:40]], "none",
                   "OPERATION_NOT_ALLOWLISTED")

    # ---------- activity / inspector ----------

    def view_activity_log(self, actor: Actor, limit: int = 200) -> list[dict]:
        """Security auditors see all (value-free) events in their audit scope.
        Everyone else sees only events where they were the actor."""
        allowed, reason, scope = self._check(actor, "view", "redacted_access_events", "DemoOrg-US")
        if allowed:
            events = self._read_jsonl(self.events_file)
        else:
            events = [e for e in self._read_jsonl(self.events_file) if e["actor_id"] == actor.user_id]
            scope, reason = "own_activity", "OWN_ACTIVITY_ONLY"
        self._log(str(uuid.uuid4()), actor.user_id, actor.role, "view", "access_events", scope, [],
                  scope, "ALLOW", reason, "succeeded")
        return events[-limit:][::-1]

    def view_change_history(self, actor: Actor) -> list[dict]:
        """Restricted before/after history. Employees see changes to their own record;
        managers see the decisions they made. No other role has access in v1."""
        if actor.role == "employee":
            rows = [h for h in self._read_jsonl(self.history_file) if h["target_employee_id"] == actor.employee_id]
        elif actor.role == "manager":
            rows = [h for h in self._read_jsonl(self.history_file) if h["approver_id"] == actor.user_id]
        else:
            self._deny(actor, "view", "change_history", "all", [], "none", "NO_MATCHING_RULE")
        self._log(str(uuid.uuid4()), actor.user_id, actor.role, "view", "change_history", "own", [],
                  "own_records", "ALLOW", "OWN_RECORDS_ONLY", "succeeded")
        return rows[::-1]

    def reset_runtime(self) -> None:
        """Demo control: discard all runtime state and go back to the seed files."""
        with self._lock:
            for f in (self.events_file, self.history_file, self.approvals_file):
                f.unlink(missing_ok=True)
            self._write_json_atomic(self.contacts_file, self._load_seed("contact_details.json"))
            self._write_json_atomic(self.leave_file, self._load_seed("leave_requests.json"))

    # ---------- internals ----------

    def _authorize(self, actor: Actor, action: str, resource: str, target: str, fields: list[str]) -> Ctx:
        allowed, reason, scope = self._check(actor, action, resource, target)
        if not allowed:
            self._deny(actor, action, resource, target, fields, scope, reason)
        ctx = Ctx(str(uuid.uuid4()), actor, action, resource, target, fields, scope, reason)
        self._log_ctx(ctx, "ALLOW", "authorized")
        return ctx

    def _deny(self, actor: Actor, action: str, resource: str, target: str, fields: list[str], scope: str, reason: str):
        # If logging fails we still deny; AuditFailure propagates instead of AccessDenied.
        self._log(str(uuid.uuid4()), actor.user_id, actor.role, action, resource, str(target)[:40], fields,
                  scope, "DENY", reason, "not_executed")
        raise AccessDenied(reason)

    def _outcome(self, ctx: Ctx, outcome: str):
        self._log_ctx(ctx, "ALLOW", outcome)

    def _log_ctx(self, ctx: Ctx, decision: str, outcome: str):
        self._log(ctx.request_id, ctx.actor.user_id, ctx.actor.role, ctx.action, ctx.resource, ctx.target_id,
                  ctx.fields, ctx.scope, decision, ctx.reason, outcome)

    def _log(self, request_id, actor_id, role, action, resource, target_id, fields, scope, decision, reason, outcome):
        # IDs and field names only, never values.
        event = {
            "event_id": str(uuid.uuid4()),
            "timestamp_utc": _now(),
            "request_id": request_id,
            "actor_id": actor_id,
            "verified_role": role,
            "action": action,
            "target_resource": resource,
            "target_id": target_id,
            "field_names": fields,
            "scope": scope,
            "decision": decision,
            "reason_code": reason,
            "execution_outcome": outcome,
        }
        try:
            self._append_jsonl(self.events_file, event)
        except OSError as exc:
            raise AuditFailure("Audit log unavailable; operation blocked") from exc

    def _history(self, ctx: Ctx, target_employee_id: str, field: str, before, after,
                 approver_id=None, approval_id=None) -> dict:
        return {
            "change_id": str(uuid.uuid4()),
            "request_id": ctx.request_id,
            "actor_id": ctx.actor.user_id,
            "executor": "system",
            "approver_id": approver_id,
            "approval_id": approval_id,
            "target_employee_id": target_employee_id,
            "field": field,
            "before_value": before,
            "after_value": after,
            "timestamp_utc": _now(),
            "outcome": "applied",
        }

    def _commit(self, ctx: Ctx, data_file: Path, data, appends: list[tuple[Path, dict]]):
        """Write the data file, then its records. If anything fails, restore the data file."""
        original = data_file.read_text()
        try:
            self._write_json_atomic(data_file, data)
            for path, record in appends:
                self._append_jsonl(path, record)
        except OSError:
            data_file.write_text(original)
            self._outcome(ctx, "failed")
            raise

    def _workforce_for(self, actor: Actor, employee_ids: list[str]) -> tuple[list[dict], str]:
        """Workforce metric rows for already-authorized employee ids, and where they came from.
        Callers must authorize every id first; ClickHouse is queried for exactly these ids."""
        if self.analytics is None:
            return self._workforce, "local"
        from people_app.analytics import SOURCE_CLICKHOUSE, SOURCE_FALLBACK
        try:
            return self.analytics.fetch(actor.user_id, actor.role, employee_ids), SOURCE_CLICKHOUSE
        except Exception:
            return [r for r in self._workforce if r["employee_id"] in set(employee_ids)], SOURCE_FALLBACK

    def _rows(self, employee_id: str, fields, data: list[dict] | None = None) -> list[dict]:
        data = self._workforce if data is None else data
        rows = [{k: r[k] for k in fields} for r in data if r["employee_id"] == employee_id]
        return sorted(rows, key=lambda r: r["month"])

    def _pto_summary(self, employee_id: str, data: list[dict] | None = None) -> dict:
        months = self._rows(employee_id, PTO_FIELDS, data)
        reserved = sum(r["hours"] for r in self._read_json(self.leave_file)
                       if r["employee_id"] == employee_id and r["status"] == "approved")
        latest = months[-1]["pto_closing_hours"] if months else 0
        return {"months": months, "reserved_hours": reserved, "available_hours": latest - reserved}

    def _validate(self, field: str, value) -> str:
        if not isinstance(value, str):
            raise ValidationError("Value must be text")
        value = " ".join(value.split())
        if field == "phone":
            digits = sum(c.isdigit() for c in value)
            if not PHONE_RE.match(value) or not 7 <= digits <= 15:
                raise ValidationError("Phone must be 7-15 digits; allowed: + space ( ) . -")
        elif field == "address":
            if not 5 <= len(value) <= 200 or not ADDRESS_RE.match(value):
                raise ValidationError("Address must be 5-200 characters of letters, numbers and , . # ' / -")
        return value

    @staticmethod
    def _find_contact(contacts: list[dict], employee_id: str) -> dict:
        for row in contacts:
            if row["employee_id"] == employee_id:
                return row
        raise AccessDenied("TARGET_NOT_FOUND")

    @staticmethod
    def _find_leave(requests: list[dict], request_id: str) -> dict | None:
        return next((r for r in requests if r["request_id"] == request_id), None)

    def _load_seed(self, name: str):
        return json.loads((self.seed_dir / name).read_text())

    @staticmethod
    def _read_json(path: Path):
        return json.loads(path.read_text())

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @staticmethod
    def _append_jsonl(path: Path, record: dict) -> None:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    @staticmethod
    def _write_json_atomic(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)
