import json
from pathlib import Path

import pytest

from people_app.service import (
    ROOT, AccessDenied, AuditFailure, PeopleService, ValidationError,
)

EVENT_FIELDS = json.loads((ROOT / "policy" / "event_schema.json").read_text())["access_event_required"]
HISTORY_FIELDS = json.loads((ROOT / "policy" / "event_schema.json").read_text())["change_history_required"]


@pytest.fixture
def svc(tmp_path):
    return PeopleService(runtime_dir=tmp_path)


@pytest.fixture
def emp01(svc):
    return svc.get_actor("U_EMP01")


def events(svc):
    return svc._read_jsonl(svc.events_file)


# ---------- viewing ----------

def test_employee_views_own_contact(svc, emp01):
    c = svc.view_contact(emp01, "E001")
    assert c["employee_id"] == "E001" and c["phone"] == "+1-202-555-0100"


def test_employee_cannot_view_other_contact(svc, emp01):
    with pytest.raises(AccessDenied) as e:
        svc.view_contact(emp01, "E011")
    assert e.value.reason_code == "OUT_OF_SCOPE"
    last = events(svc)[-1]
    assert last["decision"] == "DENY" and last["execution_outcome"] == "not_executed"


def test_employee_views_own_pto_without_overtime(svc, emp01):
    rows = svc.view_pto(emp01, "E001")["months"]
    assert [r["month"] for r in rows] == ["2026-07", "2026-08", "2026-09"]
    assert all("overtime_hours" not in r for r in rows)


def test_employee_cannot_view_other_pto(svc, emp01):
    with pytest.raises(AccessDenied):
        svc.view_pto(emp01, "E002")


def test_unknown_target_does_not_reveal_existence(svc, emp01):
    with pytest.raises(AccessDenied) as e:
        svc.view_contact(emp01, "E999")
    assert e.value.reason_code == "OUT_OF_SCOPE"


def test_non_employee_role_cannot_use_employee_self_service(svc):
    manager = svc.get_actor("M001")
    with pytest.raises(AccessDenied) as e:
        svc.view_contact(manager, "E001")
    assert e.value.reason_code == "NO_MATCHING_RULE"


def test_unknown_identity_denied_and_logged(svc):
    with pytest.raises(AccessDenied):
        svc.get_actor("ADMIN")
    assert events(svc)[-1]["reason_code"] == "UNKNOWN_IDENTITY"


# ---------- editing ----------

@pytest.mark.parametrize("field,value", [("address", "999 Example Avenue, Demo City"),
                                         ("phone", "+1-202-555-0199")])
def test_employee_edits_own_contact_with_history(svc, emp01, field, value):
    before = svc.view_contact(emp01, "E001")[field]
    result = svc.edit_contact(emp01, "E001", field, value)
    assert result["changed"]
    assert svc.view_contact(emp01, "E001")[field] == value
    [h] = svc.view_change_history(emp01)
    assert set(HISTORY_FIELDS) <= h.keys()
    assert (h["before_value"], h["after_value"], h["field"]) == (before, value, field)
    outcomes = [e["execution_outcome"] for e in events(svc) if e["request_id"] == h["request_id"]]
    assert outcomes == ["authorized", "started", "succeeded"]


def test_employee_cannot_edit_other_contact(svc, emp01):
    other = svc.get_actor("U_EMP11")
    original = svc.view_contact(other, "E011")
    with pytest.raises(AccessDenied):
        svc.edit_contact(emp01, "E011", "phone", "+1-202-555-0000")
    assert svc.view_contact(other, "E011") == original
    assert svc._read_jsonl(svc.history_file) == []


def test_employee_cannot_edit_pto(svc, emp01):
    before = svc.view_pto(emp01, "E001")
    with pytest.raises(AccessDenied) as e:
        svc.edit_pto(emp01, "E001", "pto_closing_hours", 100)
    assert e.value.reason_code == "PTO_SYSTEM_MAINTAINED"
    assert svc.view_pto(emp01, "E001") == before


@pytest.mark.parametrize("field", ["employee_id", "display_name", "annual_salary_usd", "pto_closing_hours"])
def test_non_editable_fields_denied(svc, emp01, field):
    with pytest.raises(AccessDenied):
        svc.edit_contact(emp01, "E001", field, "x")
    assert svc._read_jsonl(svc.history_file) == []


@pytest.mark.parametrize("field,value", [("phone", "call me"), ("phone", "123"),
                                         ("address", "<script>alert(1)</script>"), ("address", "")])
def test_invalid_values_rejected_without_change(svc, emp01, field, value):
    before = svc.view_contact(emp01, "E001")
    with pytest.raises(ValidationError):
        svc.edit_contact(emp01, "E001", field, value)
    assert svc.view_contact(emp01, "E001") == before
    assert svc._read_jsonl(svc.history_file) == []


def test_audit_failure_blocks_mutation(svc, emp01, monkeypatch):
    before = svc.view_contact(emp01, "E001")

    def broken(path, record):
        raise OSError("disk full")
    monkeypatch.setattr(PeopleService, "_append_jsonl", staticmethod(broken))
    with pytest.raises(AuditFailure):
        svc.edit_contact(emp01, "E001", "address", "500 Blocked Road, Demo City")
    monkeypatch.undo()
    assert svc.view_contact(emp01, "E001") == before


# ---------- logging ----------

def test_events_have_schema_fields_and_no_values(svc, emp01):
    old = svc.view_contact(emp01, "E001")
    new_addr, new_phone = "777 Secret Lane, Demo City", "+1-202-555-0777"
    svc.edit_contact(emp01, "E001", "address", new_addr)
    svc.edit_contact(emp01, "E001", "phone", new_phone)
    with pytest.raises(AccessDenied):
        svc.view_contact(emp01, "E011")
    raw = svc.events_file.read_text()
    for value in (old["address"], old["phone"], new_addr, new_phone, "+1-202-555-0110"):
        assert value not in raw
    for e in events(svc):
        assert set(EVENT_FIELDS) <= e.keys()


def test_runtime_reset_restores_seed(svc, emp01):
    svc.edit_contact(emp01, "E001", "address", "1 Changed Street, Demo City")
    svc.reset_runtime()
    assert svc.view_contact(emp01, "E001")["address"] == "100 Example Avenue, Demo City"


# ---------- provided test cases that apply to employees ----------

CASES = [c for c in json.loads((ROOT / "tests" / "test_cases.json").read_text())
         if c["test_id"] in {"T01", "T02", "T03", "T04", "T05", "T06", "T07", "T17"}]


@pytest.mark.parametrize("case", CASES, ids=[c["test_id"] for c in CASES])
def test_provided_cases(svc, case):
    actor = svc.get_actor(case["user_id"])
    value = "999 Example Avenue, Demo City" if case["field"] == "address" else "100"
    if case["expected"] == "ALLOW":
        svc.perform(actor, case["action"], case["target"], case["field"], value)
    else:
        with pytest.raises(AccessDenied):
            svc.perform(actor, case["action"], case["target"], case["field"], value)
        assert events(svc)[-1]["decision"] == "DENY"
