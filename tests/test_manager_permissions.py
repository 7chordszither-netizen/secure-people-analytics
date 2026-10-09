import pytest

from people_app.service import AccessDenied, ConflictError, PeopleService

TEAM_M001 = {f"E{n:03d}" for n in range(1, 11)}
TEAM_M002 = {f"E{n:03d}" for n in range(11, 21)}


@pytest.fixture
def svc(tmp_path):
    return PeopleService(runtime_dir=tmp_path)


@pytest.fixture
def m1(svc):
    return svc.get_actor("M001")


@pytest.fixture
def m2(svc):
    return svc.get_actor("M002")


def events(svc):
    return svc._read_jsonl(svc.events_file)


def leave_status(svc, request_id):
    return next(r["status"] for r in svc._read_json(svc.leave_file) if r["request_id"] == request_id)


# ---------- team view ----------

def test_manager_sees_only_direct_reports_with_overtime(svc, m1):
    rows = svc.view_team_workforce(m1)
    assert {r["employee_id"] for r in rows} == TEAM_M001
    assert len(rows) == 30 and all("overtime_hours" in r for r in rows)


def test_second_manager_sees_only_their_team(svc, m2):
    assert {r["employee_id"] for r in svc.view_team_workforce(m2)} == TEAM_M002


@pytest.mark.parametrize("method", ["view_pto", "view_overtime"])
@pytest.mark.parametrize("manager,target", [("M001", "E011"), ("M001", "E020"), ("M002", "E001")])
def test_cross_team_access_denied(svc, method, manager, target):
    with pytest.raises(AccessDenied) as e:
        getattr(svc, method)(svc.get_actor(manager), target)
    assert e.value.reason_code == "OUT_OF_SCOPE"
    assert events(svc)[-1]["decision"] == "DENY"


def test_manager_views_single_direct_report(svc, m1):
    assert svc.view_pto(m1, "E005")["months"]
    assert svc.view_overtime(m1, "E005")


def test_manager_cannot_view_team_contact_details(svc, m1):
    with pytest.raises(AccessDenied) as e:
        svc.view_contact(m1, "E001")
    assert e.value.reason_code == "NO_MATCHING_RULE"


def test_employee_cannot_use_team_view(svc):
    with pytest.raises(AccessDenied):
        svc.view_team_workforce(svc.get_actor("U_EMP01"))


def test_team_membership_requires_reporting_line(tmp_path, monkeypatch):
    # If a manager's scope list contained someone who doesn't report to them, they stay hidden.
    svc = PeopleService(runtime_dir=tmp_path)
    svc._employees["E002"] = {**svc._employees["E002"], "manager_id": "M002"}
    rows = svc.view_team_workforce(svc.get_actor("M001"))
    assert "E002" not in {r["employee_id"] for r in rows}
    with pytest.raises(AccessDenied):
        svc.view_pto(svc.get_actor("M001"), "E002")


# ---------- leave decisions ----------

def test_manager_lists_only_assigned_requests(svc, m1, m2):
    assert [r["request_id"] for r in svc.list_leave_requests(m1)] == ["L001"]
    assert [r["request_id"] for r in svc.list_leave_requests(m2)] == ["L002"]


def test_approve_assigned_request_reserves_pto_once(svc, m1):
    emp = svc.get_actor("U_EMP01")
    before = svc.view_pto(emp, "E001")
    result = svc.decide_leave(m1, "L001", "approve")
    assert result["status"] == "approved" and leave_status(svc, "L001") == "approved"

    after = svc.view_pto(emp, "E001")
    assert after["months"] == before["months"]            # system history untouched
    assert after["reserved_hours"] == 8
    assert after["available_hours"] == before["available_hours"] - 8

    [approval] = svc._read_jsonl(svc.approvals_file)
    assert approval["approver_id"] == "M001" and approval["request_fingerprint"]
    [h] = svc.view_change_history(m1)
    assert (h["before_value"], h["after_value"], h["approval_id"]) == ("pending", "approved", approval["approval_id"])
    outcomes = [e["execution_outcome"] for e in events(svc) if e["request_id"] == h["request_id"]]
    assert outcomes == ["authorized", "started", "succeeded"]


def test_deny_assigned_request(svc, m2):
    svc.decide_leave(m2, "L002", "deny")
    assert leave_status(svc, "L002") == "denied"
    assert svc.view_pto(svc.get_actor("U_EMP11"), "E011")["reserved_hours"] == 0


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_unauthorized_approval_other_team(svc, m1, decision):
    with pytest.raises(AccessDenied) as e:
        svc.decide_leave(m1, "L002", decision)
    assert e.value.reason_code == "OUT_OF_SCOPE"
    assert leave_status(svc, "L002") == "pending"
    assert svc._read_jsonl(svc.approvals_file) == []


@pytest.mark.parametrize("user", ["U_EMP01", "U_EMP02", "U_EMP11"])
def test_employees_cannot_approve_leave(svc, user):
    with pytest.raises(AccessDenied):
        svc.decide_leave(svc.get_actor(user), "L001", "approve")
    with pytest.raises(AccessDenied):
        svc.list_leave_requests(svc.get_actor(user))
    assert leave_status(svc, "L001") == "pending"


def test_request_decided_only_once(svc, m1):
    svc.decide_leave(m1, "L001", "approve")
    with pytest.raises(ConflictError):
        svc.decide_leave(m1, "L001", "deny")
    assert leave_status(svc, "L001") == "approved"
    assert len(svc._read_jsonl(svc.approvals_file)) == 1


@pytest.mark.parametrize("request_id", ["L999", "", "L001' OR 1=1"])
def test_unknown_request_denied(svc, m1, request_id):
    with pytest.raises(AccessDenied):
        svc.decide_leave(m1, request_id, "approve")


@pytest.mark.parametrize("decision", ["delete", "edit", "approve_all"])
def test_only_approve_or_deny(svc, m1, decision):
    with pytest.raises(AccessDenied) as e:
        svc.decide_leave(m1, "L001", decision)
    assert e.value.reason_code == "OPERATION_NOT_ALLOWLISTED"
    assert leave_status(svc, "L001") == "pending"


# ---------- direct edits and sharing ----------

def test_manager_cannot_edit_team_records(svc, m1):
    with pytest.raises(AccessDenied):
        svc.edit_pto(m1, "E001", "pto_closing_hours", 200)
    with pytest.raises(AccessDenied):
        svc.edit_workforce(m1, "E001", "overtime_hours", 0)
    with pytest.raises(AccessDenied):
        svc.edit_contact(m1, "E001", "phone", "+1-202-555-0000")
    assert svc._read_jsonl(svc.history_file) == []


@pytest.mark.parametrize("user", ["M001", "M002", "U_EMP01"])
@pytest.mark.parametrize("action", ["share", "export"])
def test_sharing_and_export_denied(svc, user, action):
    with pytest.raises(AccessDenied) as e:
        svc.perform(svc.get_actor(user), action, "E001", "workforce")
    assert e.value.reason_code == "SHARING_DISABLED"


# ---------- inspector restrictions ----------

def test_employee_activity_log_shows_only_own_events(svc, m1):
    e1, e11 = svc.get_actor("U_EMP01"), svc.get_actor("U_EMP11")
    svc.view_contact(e11, "E011")
    svc.edit_contact(e11, "E011", "phone", "+1-202-555-0911")
    svc.decide_leave(m1, "L001", "approve")
    svc.view_contact(e1, "E001")
    log = svc.view_activity_log(e1)
    assert log and {e["actor_id"] for e in log} == {"U_EMP01"}


def test_employee_change_history_shows_only_own_record(svc, m1):
    e1, e11 = svc.get_actor("U_EMP01"), svc.get_actor("U_EMP11")
    svc.edit_contact(e11, "E011", "phone", "+1-202-555-0911")
    svc.edit_contact(e1, "E001", "phone", "+1-202-555-0901")
    assert {h["target_employee_id"] for h in svc.view_change_history(e1)} == {"E001"}
    assert {h["target_employee_id"] for h in svc.view_change_history(e11)} == {"E011"}
    assert svc.view_change_history(m1) == []        # manager didn't make those changes


def test_manager_history_shows_only_their_decisions(svc, m1, m2):
    svc.decide_leave(m1, "L001", "approve")
    svc.decide_leave(m2, "L002", "deny")
    assert [h["approver_id"] for h in svc.view_change_history(m1)] == ["M001"]
    assert [h["approver_id"] for h in svc.view_change_history(m2)] == ["M002"]


def test_auditor_sees_all_events_but_no_change_history(svc, m1):
    svc.view_contact(svc.get_actor("U_EMP01"), "E001")
    svc.decide_leave(m1, "L001", "approve")
    auditor = svc.get_actor("S001")
    assert {"U_EMP01", "M001"} <= {e["actor_id"] for e in svc.view_activity_log(auditor)}
    with pytest.raises(AccessDenied):
        svc.view_change_history(auditor)
