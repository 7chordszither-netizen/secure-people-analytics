import json
import logging

import pytest

from people_app import analytics
from people_app.analytics import SOURCE_CLICKHOUSE, SOURCE_FALLBACK, ClickHouseWorkforce, source_rows
from people_app.clickhouse_db import ConfigError, get_client, load_settings
from people_app.service import ROOT, AccessDenied, PeopleService

TEAM_M001 = {f"E{n:03d}" for n in range(1, 11)}
TEAM_M002 = {f"E{n:03d}" for n in range(11, 21)}
FORBIDDEN = {"display_name", "name", "phone", "address", "salary", "compensation", "succession", "investigation"}
SEED_WORKFORCE = json.loads((ROOT / "data" / "workforce_monthly.json").read_text())


# ---------- without ClickHouse ----------

class FakeClickHouse:
    """Records every query; returns seed rows for exactly the requested ids."""

    def __init__(self):
        self.calls = []

    def fetch(self, user_id, role, employee_ids):
        self.calls.append((user_id, role, tuple(sorted(employee_ids))))
        return [dict(r) for r in SEED_WORKFORCE if r["employee_id"] in set(employee_ids)]


class DownClickHouse:
    def fetch(self, *a):
        raise ConnectionError("down")


def test_upload_rows_contain_only_allowed_columns():
    rows = source_rows()
    assert len(rows) == len(SEED_WORKFORCE)
    for r in rows:
        assert set(r) == set(analytics.COLUMNS)
        assert not FORBIDDEN & set(r)


def test_denied_request_never_queries_analytics(tmp_path):
    fake = FakeClickHouse()
    svc = PeopleService(runtime_dir=tmp_path, analytics=fake)
    with pytest.raises(AccessDenied):
        svc.view_pto(svc.get_actor("U_EMP01"), "E011")
    with pytest.raises(AccessDenied):
        svc.view_team_analytics(svc.get_actor("U_EMP01"))
    assert fake.calls == []


def test_queries_are_limited_to_authorized_ids(tmp_path):
    fake = FakeClickHouse()
    svc = PeopleService(runtime_dir=tmp_path, analytics=fake)
    svc.view_pto(svc.get_actor("U_EMP01"), "E001")
    svc.view_team_analytics(svc.get_actor("M001"))
    assert fake.calls == [("U_EMP01", "employee", ("E001",)),
                          ("M001", "manager", tuple(sorted(TEAM_M001)))]


def test_fallback_is_labelled_and_still_scoped(tmp_path):
    svc = PeopleService(runtime_dir=tmp_path, analytics=DownClickHouse())
    team = svc.view_team_analytics(svc.get_actor("M002"))
    assert team["source"] == SOURCE_FALLBACK
    assert {r["employee_id"] for r in team["rows"]} == TEAM_M002
    assert svc.view_pto(svc.get_actor("U_EMP01"), "E001")["source"] == SOURCE_FALLBACK


def test_cache_is_keyed_by_identity_and_permitted_ids():
    class Client:
        def __init__(self):
            self.queries = 0

        def query(self, sql, parameters):
            self.queries += 1

            class R:
                column_names = ["employee_id", "month"] + list(analytics.METRICS)
                result_rows = [(r["employee_id"], r["month"], *[r[m] for m in analytics.METRICS])
                               for r in SEED_WORKFORCE if r["employee_id"] in parameters["ids"]]
            return R()

    client = Client()
    reader = ClickHouseWorkforce(client_factory=lambda: client)
    m1 = reader.fetch("M001", "manager", sorted(TEAM_M001))
    m2 = reader.fetch("M002", "manager", sorted(TEAM_M002))
    assert {r["employee_id"] for r in m1} == TEAM_M001
    assert {r["employee_id"] for r in m2} == TEAM_M002
    assert reader.fetch("M001", "manager", sorted(TEAM_M001)) == m1
    assert client.queries == 2  # third call was a cache hit for M001 only
    # Same ids under a different identity is a different cache entry, not a shared one.
    reader.fetch("U_EMP01", "employee", ["E001"])
    assert client.queries == 3
    assert set(reader._cache) == {("M001", "manager", tuple(sorted(TEAM_M001))),
                                  ("M002", "manager", tuple(sorted(TEAM_M002))),
                                  ("U_EMP01", "employee", ("E001",))}


# ---------- against the real ClickHouse table ----------

def _clickhouse_ready() -> bool:
    try:
        load_settings()
        return True
    except ConfigError:
        return False


clickhouse = pytest.mark.skipif(not _clickhouse_ready(), reason="ClickHouse not configured in .env")


@pytest.fixture(scope="module")
def ch_client():
    logging.getLogger("clickhouse_connect").setLevel(logging.CRITICAL)
    client = get_client(timeout=60)
    yield client
    client.close()


@pytest.fixture
def ch_svc(tmp_path):
    reader = ClickHouseWorkforce(client_factory=lambda: get_client(timeout=60))
    return PeopleService(runtime_dir=tmp_path, analytics=reader), reader


@clickhouse
def test_uploaded_count_matches_source_and_repeat_is_idempotent(ch_client):
    first = analytics.upload(ch_client)
    assert first["table_rows"] == first["source_rows"] == len(SEED_WORKFORCE)
    second = analytics.upload(ch_client)
    assert second["inserted"] == 0
    assert second["table_rows"] == first["table_rows"]
    assert int(ch_client.command(f"SELECT count() FROM {analytics.TABLE}")) == len(SEED_WORKFORCE)


@clickhouse
def test_e001_cannot_retrieve_e011(ch_svc):
    svc, _ = ch_svc
    e001 = svc.get_actor("U_EMP01")
    for call in (lambda: svc.view_pto(e001, "E011"), lambda: svc.view_overtime(e001, "E011"),
                 lambda: svc.view_team_analytics(e001)):
        with pytest.raises(AccessDenied):
            call()
    own = svc.view_pto(e001, "E001")
    assert own["source"] == SOURCE_CLICKHOUSE
    assert len(own["months"]) == 3


@clickhouse
@pytest.mark.parametrize("manager, team", [("M001", TEAM_M001), ("M002", TEAM_M002)])
def test_manager_sees_only_direct_reports(ch_svc, manager, team):
    svc, _ = ch_svc
    result = svc.view_team_analytics(svc.get_actor(manager))
    assert result["source"] == SOURCE_CLICKHOUSE
    assert {r["employee_id"] for r in result["rows"]} == team
    assert len(result["rows"]) == 30
    local = {(r["employee_id"], r["month"]): r for r in SEED_WORKFORCE}
    for r in result["rows"]:  # ClickHouse values match the source data
        src = local[(r["employee_id"], r["month"])]
        assert r["pto_closing_hours"] == src["pto_closing_hours"]
        assert r["overtime_hours"] == src["overtime_hours"]


@clickhouse
def test_switching_users_never_returns_another_users_cached_rows(ch_svc):
    svc, reader = ch_svc
    sequence = [("M001", TEAM_M001), ("M002", TEAM_M002), ("U_EMP01", {"E001"}),
                ("U_EMP11", {"E011"}), ("M001", TEAM_M001), ("M002", TEAM_M002), ("U_EMP01", {"E001"})]
    for user_id, expected in sequence:
        actor = svc.get_actor(user_id)
        if actor.role == "manager":
            result = svc.view_team_analytics(actor)
            seen = {r["employee_id"] for r in result["rows"]}
        else:
            result = svc.view_pto(actor, actor.employee_id)
            seen = {actor.employee_id} if result["months"] else set()
        assert result["source"] == SOURCE_CLICKHOUSE, user_id
        assert seen == expected, user_id
    assert all(key[0] in {"M001", "M002", "U_EMP01", "U_EMP11"} for key in reader._cache)
    assert len(reader._cache) == 4
