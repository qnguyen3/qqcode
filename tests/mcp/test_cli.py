from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
import threading
import tomllib
import webbrowser

import httpx
import pytest
import tomli_w

from tests.mcp.fake_oauth_server import FakeOAuthServer
from tests.mcp.helpers import FAKE_SERVER
from vibe.cli import mcp_cli
from vibe.core import config as config_module
from vibe.core.config import VibeConfig


@pytest.fixture
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Callable[[], VibeConfig]]:
    config_file = tmp_path / "config.toml"
    config_file.write_text('active_model = "devstral-2"\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)

    def real_dump(cls: type[VibeConfig], payload: dict) -> None:
        with config_file.open("wb") as handle:
            tomli_w.dump(payload, handle)

    monkeypatch.setattr(VibeConfig, "dump_config", classmethod(real_dump))

    def load() -> VibeConfig:
        local = Path.cwd() / ".qqcode" / "config.toml"
        path = local if local.is_file() else config_file
        return VibeConfig.model_validate(tomllib.loads(path.read_text("utf-8")))

    monkeypatch.setattr(mcp_cli, "_load_config", load)
    return config_file, load


def _read_servers(config_file: Path) -> list[dict]:
    data = tomllib.loads(config_file.read_text("utf-8"))
    return data.get("mcp_servers", [])


def test_add_stdio_verifies_and_writes(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    config_file, load = cli_env

    rc = mcp_cli.run_mcp_command([
        "add",
        "fake",
        "--timeout",
        "5",
        "--",
        sys.executable,
        str(FAKE_SERVER),
    ])

    assert rc == 0
    output = capsys.readouterr().out
    assert "tool(s)" in output
    servers = load().mcp_servers
    assert [server.name for server in servers] == ["fake"]
    assert servers[0].transport == "stdio"
    assert _read_servers(config_file)[0]["command"] == sys.executable


def test_add_url_failure_does_not_write(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    config_file, _ = cli_env

    rc = mcp_cli.run_mcp_command([
        "add",
        "bad",
        "--url",
        "http://127.0.0.1:9/mcp",
        "--timeout",
        "1",
    ])

    assert rc == 1
    assert "Could not connect" in capsys.readouterr().err
    assert _read_servers(config_file) == []


def test_add_no_verify_skips_probe(
    cli_env: tuple[Path, Callable[[], VibeConfig]],
) -> None:
    config_file, _ = cli_env

    rc = mcp_cli.run_mcp_command([
        "add",
        "offline",
        "--no-verify",
        "--url",
        "http://example.invalid/mcp",
    ])

    assert rc == 0
    assert _read_servers(config_file)[0]["name"] == "offline"


def test_add_stdio_env_is_written(
    cli_env: tuple[Path, Callable[[], VibeConfig]],
) -> None:
    config_file, _ = cli_env

    rc = mcp_cli.run_mcp_command([
        "add",
        "envs",
        "--no-verify",
        "--env",
        "API_TOKEN=abc",
        "--",
        sys.executable,
        str(FAKE_SERVER),
    ])

    assert rc == 0
    assert _read_servers(config_file)[0]["env"] == {"API_TOKEN": "abc"}


def test_add_http_env_is_rejected(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    config_file, _ = cli_env

    rc = mcp_cli.run_mcp_command([
        "add",
        "bad",
        "--no-verify",
        "--env",
        "A=B",
        "--url",
        "http://example.invalid/mcp",
    ])

    assert rc == 1
    assert "only supported for stdio" in capsys.readouterr().err
    assert _read_servers(config_file) == []


def test_add_replaces_existing_server(
    cli_env: tuple[Path, Callable[[], VibeConfig]],
) -> None:
    config_file, _ = cli_env
    base = ["add", "dup", "--no-verify", "--url"]

    assert mcp_cli.run_mcp_command([*base, "http://one.invalid/mcp"]) == 0
    assert mcp_cli.run_mcp_command([*base, "http://two.invalid/mcp"]) == 0

    servers = _read_servers(config_file)
    assert len(servers) == 1
    assert servers[0]["url"] == "http://two.invalid/mcp"


def test_list_json_reports_status(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    _, load = cli_env
    assert (
        mcp_cli.run_mcp_command([
            "add",
            "fake",
            "--timeout",
            "5",
            "--",
            sys.executable,
            str(FAKE_SERVER),
        ])
        == 0
    )
    capsys.readouterr()

    rc = mcp_cli.run_mcp_command(["list", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    entry = next(item for item in payload if item["name"] == "fake")
    assert entry["connected"] is True
    assert entry["tools"] == 4
    assert load().mcp_servers[0].tool_exposure == "direct"


def test_remove_server(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    config_file, _ = cli_env
    assert (
        mcp_cli.run_mcp_command([
            "add",
            "gone",
            "--no-verify",
            "--url",
            "http://example.invalid/mcp",
        ])
        == 0
    )

    assert mcp_cli.run_mcp_command(["remove", "gone"]) == 0
    assert _read_servers(config_file) == []
    assert mcp_cli.run_mcp_command(["remove", "gone"]) == 1
    assert "not found" in capsys.readouterr().err


def test_remove_local(
    cli_env: tuple[Path, Callable[[], VibeConfig]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file, _ = cli_env
    local_dir = tmp_path / "project" / ".qqcode"
    local_dir.mkdir(parents=True)
    local_file = local_dir / "config.toml"
    local_file.write_text(
        'active_model = "devstral-2"\n\n'
        '[[mcp_servers]]\nname = "local-one"\ntransport = "stdio"\n'
        'command = "echo"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(local_dir.parent)

    assert mcp_cli.run_mcp_command(["remove", "local-one", "--local"]) == 0
    assert tomllib.loads(local_file.read_text())["mcp_servers"] == []
    assert "mcp_servers" not in tomllib.loads(config_file.read_text())


def test_login_requires_oauth_configuration(
    cli_env: tuple[Path, Callable[[], VibeConfig]], capsys: pytest.CaptureFixture
) -> None:
    config_file, _ = cli_env
    config_file.write_text(
        'active_model = "devstral-2"\n\n'
        '[[mcp_servers]]\nname = "plain"\ntransport = "streamable-http"\n'
        'url = "http://example.invalid/mcp"\n',
        encoding="utf-8",
    )

    assert mcp_cli.run_mcp_command(["login", "plain"]) == 1
    assert "no oauth configuration" in capsys.readouterr().err


def test_login_and_logout_with_fake_oauth(
    cli_env: tuple[Path, Callable[[], VibeConfig]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    fake = FakeOAuthServer()
    config_file, _ = cli_env
    config_file.write_text(
        'active_model = "devstral-2"\n\n'
        '[[mcp_servers]]\nname = "protected"\ntransport = "streamable-http"\n'
        f'url = "{fake.mcp_url}"\n\n'
        "[mcp_servers.oauth]\n",
        encoding="utf-8",
    )
    token_path = tmp_path / "mcp-auth.json"
    monkeypatch.setattr("vibe.core.mcp.oauth.TOKEN_STORE_PATH", token_path)

    def fake_open(url: str) -> bool:
        def _follow() -> None:
            httpx.get(url, timeout=10, follow_redirects=True)

        threading.Thread(target=_follow, daemon=True).start()
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    try:
        assert mcp_cli.run_mcp_command(["login", "protected"]) == 0
        assert token_path.is_file()
        assert token_path.stat().st_mode & 0o777 == 0o600
        capsys.readouterr()

        assert mcp_cli.run_mcp_command(["logout", "protected"]) == 0
        assert "Removed stored credentials" in capsys.readouterr().out
    finally:
        fake.close()
