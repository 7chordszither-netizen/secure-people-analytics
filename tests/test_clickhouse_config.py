import pytest

from people_app.clickhouse_db import ROOT, ConfigError, load_settings

BASE = ("CLICKHOUSE_HOST=h.example\nCLICKHOUSE_PORT=8443\nCLICKHOUSE_USER=default\n"
        "CLICKHOUSE_SECURE=true\n")


def test_blank_password_is_rejected(tmp_path):
    env = tmp_path / ".env"
    env.write_text(BASE + "CLICKHOUSE_PASSWORD=\n")
    with pytest.raises(ConfigError, match="CLICKHOUSE_PASSWORD"):
        load_settings(env, environ={})


def test_settings_read_from_file_and_env_overrides(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\n" + BASE + "CLICKHOUSE_PASSWORD='p=w'\n")
    s = load_settings(env, environ={"CLICKHOUSE_PORT": "9440"})
    assert s["CLICKHOUSE_PASSWORD"] == "p=w"
    assert s["CLICKHOUSE_PORT"] == "9440"


def test_env_file_is_ignored():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines and "!.env.example" in lines


def test_example_has_no_real_password():
    example = (ROOT / ".env.example").read_text()
    assert "CLICKHOUSE_PASSWORD=your-password-here" in example
