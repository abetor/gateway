"""CLI smoke tests: doctor is read-only and invalid flags exit with status 1.

Live channel checks are intentionally outside the hermetic suite.
"""
import pytest

from _helpers import fake_rig
from tool_gateway.cli import EXIT_FAIL, EXIT_OK, doctor, main


def test_doctor_ok_with_minimal_config(tmp_path, capsys, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    (home / "gateway.toml").write_text("[channels.webhook]\n", "utf-8")
    monkeypatch.setenv("GATEWAY_TOKEN", "x" * 16)
    assert main(["--home", str(home), "doctor"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "READY" in out and "x" * 16 not in out     # Never print the secret value.


def test_doctor_missing_home_fails_and_does_not_create(tmp_path, capsys):
    home = tmp_path / "no-such-home"
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    assert not home.exists()  # Doctor diagnoses without mutation.


def test_doctor_reports_missing_config(tmp_path, capsys):
    home = tmp_path / "data"
    home.mkdir()
    assert main(["--home", str(home), "doctor"]) == EXIT_FAIL
    assert "gateway.toml" in capsys.readouterr().out


def test_usage_error_exits_1(capsys):
    """The exit contract maps invalid arguments to 1 instead of argparse's 2."""
    with pytest.raises(SystemExit) as e:
        main(["--home", "x", "doctor", "--no-such-flag"])
    assert e.value.code == EXIT_FAIL
    assert "error" in capsys.readouterr().err


def test_watch_refuses_deliver_only_config(tmp_path, capsys, monkeypatch):
    """Watch fails closed without a dispatch target while delivery remains valid."""
    home = tmp_path / "data"
    home.mkdir()
    (home / "gateway.toml").write_text("[channels.telegram]\n", "utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")

    def _boom(*a, **kw):                           # The polling loop must not be reached.
        raise AssertionError("watch started without rig/projects")

    monkeypatch.setattr("tool_gateway.ext.telegram.serve.serve", _boom)
    assert main(["--home", str(home), "watch"]) == EXIT_FAIL
    assert "enables delivery only" in capsys.readouterr().err


def test_watch_accepts_generic_project_without_rig(tmp_path, capsys, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    (home / "gateway.toml").write_text('''
[channels.telegram]
[targets.runtime]
argv = ["python3", "target.py"]
[projects.events]
target = "runtime"
''', "utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    monkeypatch.setattr(
        "tool_gateway.ext.telegram.channel.TelegramChannel._urllib_http",
        lambda self, method, payload: {"ok": True, "result": []},
    )

    assert main(["--home", str(home), "watch", "--once"]) == EXIT_OK
    assert "processed messages: 0" in capsys.readouterr().out


def test_deliver_works_with_stale_watch_paths(tmp_path, capsys, monkeypatch):
    """Stale watch paths do not break the independent outbound delivery channel."""
    home = tmp_path / "data"
    home.mkdir()
    stale_rig = fake_rig(tmp_path)
    stale_project = tmp_path / "project"
    stale_project.mkdir()
    (home / "gateway.toml").write_text(f'''
[channels.telegram]
rig = "{stale_rig}"
[projects.alpha]
path = "{stale_project}"
''', "utf-8")
    (tmp_path / "rig").rename(tmp_path / "rig-renamed")
    stale_project.rmdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    monkeypatch.setattr(
        "tool_gateway.ext.telegram.channel.TelegramChannel._urllib_http",
        lambda self, method, payload: {"ok": True},
    )

    assert main(["--home", str(home), "deliver", "important notice"]) == EXIT_OK
    assert "delivered" in capsys.readouterr().out


@pytest.mark.parametrize("stale", ["rig", "project"])
def test_watch_refuses_stale_dependencies(tmp_path, capsys, monkeypatch, stale):
    """Watch keeps fail-closed filesystem checks for its session target."""
    home = tmp_path / "data"
    home.mkdir()
    rig = fake_rig(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (home / "gateway.toml").write_text(f'''
[channels.telegram]
rig = "{rig}"
[projects.alpha]
path = "{project}"
''', "utf-8")
    if stale == "rig":
        (tmp_path / "rig").rename(tmp_path / "rig-renamed")
    else:
        project.rmdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")

    assert main(["--home", str(home), "watch", "--once"]) == EXIT_FAIL
    err = capsys.readouterr().err
    expected = "does not look like a rig repository" if stale == "rig" else "path must exist"
    assert expected in err


def test_doctor_telegram_live_ping_catches_dead_token(tmp_path, capsys, monkeypatch):
    home = tmp_path / "data"
    home.mkdir()
    rig = fake_rig(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    (home / "gateway.toml").write_text(f'''
[channels.telegram]
rig = "{rig}"
[projects.alpha]
path = "{proj}"
''', "utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "714")
    # Doctor's live getMe ping exposes an expired token.
    rc = doctor(home, http=lambda m, p: {"ok": False, "description": "Unauthorized"})
    assert rc == EXIT_FAIL and "Unauthorized" in capsys.readouterr().out
    assert doctor(home, http=lambda m, p: {"ok": True}) == EXIT_OK
