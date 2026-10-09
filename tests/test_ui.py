import re

import pytest
from streamlit.testing.v1 import AppTest

from people_app import service
from people_app.service import ROOT

APP = (ROOT / "app.py").read_text()

# Streamlit elements that render a toolbar with download/export/fullscreen controls.
TOOLBAR_CALL = re.compile(r"st\.(\w+_chart|dataframe|data_editor|pyplot|pydeck_chart)\(")


def test_no_elements_with_download_toolbar():
    assert TOOLBAR_CALL.findall(APP) == []


def test_no_runtime_reset_in_ui():
    assert "reset_runtime" not in APP


def test_unauthorized_leave_button_label():
    assert '"Try unauthorized leave approval"' in APP
    assert "Approve my leave" not in APP


@pytest.fixture
def app_with_tmp_runtime(tmp_path, monkeypatch):
    # Point the app's PeopleService at a temp dir so rendering never touches runtime/ or its audit log.
    defaults = list(service.PeopleService.__init__.__defaults__)
    defaults[1] = tmp_path
    monkeypatch.setattr(service.PeopleService.__init__, "__defaults__", tuple(defaults))
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "")  # render with local data; ClickHouse is tested separately
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)


@pytest.mark.parametrize("user_id, charts", [("U_EMP18", 0), ("M001", 2)])
def test_rendered_views_have_no_toolbar_elements(app_with_tmp_runtime, user_id, charts):
    at = app_with_tmp_runtime.run()
    at.sidebar.selectbox[0].set_value(user_id).run()
    assert not at.exception
    for kind in ("arrow_vega_lite_chart", "arrow_data_frame", "dataframe", "plotly_chart", "deck_gl_json_chart"):
        assert len(at.get(kind)) == 0, kind
    assert len(at.table) > 0
    assert sum("data:image/svg+xml" in h.proto.body for h in at.get("html")) == charts
    assert at.sidebar.button.len == 0
