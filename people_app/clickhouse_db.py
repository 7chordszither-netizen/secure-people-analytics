"""ClickHouse connectivity. Settings come from the environment or the ignored .env file.

Run `python -m people_app.clickhouse_db` to check the connection with SELECT 1.
No tables are created and no data is sent. The password is never printed.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
REQUIRED = ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")


class ConfigError(Exception):
    pass


def load_settings(env_file: Path = ENV_FILE, environ=None) -> dict[str, str]:
    """Read KEY=VALUE lines from env_file; real environment variables take precedence."""
    settings: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip().strip("'\"")
    environ = os.environ if environ is None else environ
    settings.update({k: v for k, v in environ.items() if k.startswith("CLICKHOUSE_")})
    missing = [k for k in REQUIRED if not settings.get(k)]
    if missing:
        raise ConfigError(f"Missing or blank in .env: {', '.join(missing)}")
    return settings


def get_client(settings: dict[str, str] | None = None, timeout: int = 15):
    import clickhouse_connect

    s = settings or load_settings()
    return clickhouse_connect.get_client(
        host=s["CLICKHOUSE_HOST"],
        port=int(s["CLICKHOUSE_PORT"]),
        username=s["CLICKHOUSE_USER"],
        password=s["CLICKHOUSE_PASSWORD"],
        secure=s.get("CLICKHOUSE_SECURE", "true").lower() != "false",
        verify=True,
        connect_timeout=5,
        send_receive_timeout=timeout,
    )


def check_connection() -> int:
    """Return 1 if SELECT 1 succeeds. Errors are reported by type only, so no secret can leak."""
    client = get_client()
    try:
        return client.command("SELECT 1")
    finally:
        client.close()


def main() -> int:
    try:
        s = load_settings()
    except ConfigError as e:
        print(f"Not checked: {e}")
        return 2
    try:
        result = check_connection()
    except Exception as e:  # never echo the message: driver errors can include connection details
        print(f"FAILED: could not run SELECT 1 on {s['CLICKHOUSE_HOST']}:{s['CLICKHOUSE_PORT']} "
              f"({type(e).__name__})")
        return 1
    print(f"OK: SELECT 1 returned {result} from {s['CLICKHOUSE_HOST']}:{s['CLICKHOUSE_PORT']} over TLS")
    return 0 if result == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
