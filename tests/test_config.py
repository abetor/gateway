"""Gateway configuration defaults, fail-closed validation, and env resolution."""
from __future__ import annotations

import os

import pytest

from _helpers import fake_rig
from tool_gateway.core import config as cfgmod


def _write(home, body: str):
    (home / "gateway.toml").write_text(body, "utf-8")
    return home


def test_webhook_defaults_and_routes(tmp_path):
    _write(tmp_path, '''
[channels.webhook]

[routes.research-done]
command = "echo done"
''')
    c = cfgmod.load_config(tmp_path)
    assert c.webhook.host == "127.0.0.1" and c.webhook.port == 8787
    assert c.webhook.token_env == "GATEWAY_TOKEN"
    assert c.webhook.routes == {"research-done": "echo done"}
    assert c.telegram is None and c.linear is None


def test_telegram_loads_and_single_project_is_implicit_default(tmp_path):
    rig = fake_rig(tmp_path)
    (tmp_path / "proj").mkdir()
    _write(tmp_path, f'''
[channels.telegram]
rig = "{rig}"

[projects.alpha]
path = "{tmp_path / 'proj'}"
args = ["--worker", "codex"]
''')
    c = cfgmod.load_config(tmp_path)
    assert c.telegram.default == "alpha"
    assert c.telegram.projects["alpha"].args == ["--worker", "codex"]
    assert c.telegram.token_env == "TELEGRAM_BOT_TOKEN"
    assert c.telegram.chat_id_env == "TELEGRAM_CHAT_ID"


def test_config_fail_closed_on_garbage(tmp_path):
    rig = fake_rig(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    tg = f'[channels.telegram]\nrig = "{rig}"\n[projects.a]\npath = "{proj}"\n'
    cases = [
        ("[wat]\nx = 1\n", "unknown fields"),                       # Foreign top-level section.
        ("[channels.smoke]\n", "unknown channels"),
        ("[channels.webhook]\nretries = 3\n", "unknown fields"),
        ('[channels.webhook]\n[routes.x]\ncommand = "true"\nwat = 1\n', "unknown fields"),
        ("[channels.webhook]\n[routes.x]\n", "command is required"),
        ('[routes.x]\ncommand = "true"\n', "requires \\[channels.webhook\\]"),
        (f'[projects.a]\npath = "{proj}"\n', "requires \\[channels.telegram\\]"),
        (f'[channels.telegram]\nrig = "{rig}"\nfoo = 1\n[projects.a]\npath = "{proj}"\n',
         "unknown fields"),                                          # Unknown Telegram field.
        (f'[channels.telegram]\n[projects.a]\npath = "{proj}"\n', "requires \\[channels.telegram\\].rig"),
        (f'[channels.telegram]\nrig = 42\n[projects.a]\npath = "{proj}"\n',
         "expected a string"),
        (f'[channels.telegram]\nrig = "{rig}"\n[projects.a]\npath = 42\n',
         "path must be a string"),
        (f'[channels.telegram]\nrig = "{rig}"\n[projects.a]\npath = "{proj}"\nargs = "x"\n',
         "list of strings"),
        (f'[channels.telegram]\nrig = "{rig}"\ndefault = "ghost"\n'
         f'[projects.a]\npath = "{proj}"\n', "is not declared"),
        (f'[channels.telegram]\nrig = "{rig}"\n[projects.a]\npath = "{proj}"\nfilter = "x"\n',
         "unknown fields"),                                          # Unknown project field.
        ("", "enable \\[channels.webhook\\]"),
        ('[channels.linear]\ntoken_env = "L"\n', "linear is only a placeholder"),
        (tg + '[channels.linear]\nboard = "x"\n', "unknown fields"),
    ]
    for body, needle in cases:
        _write(tmp_path, body)
        with pytest.raises(ValueError, match=needle):
            cfgmod.load_config(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "channels = []\n",
        "[channels]\ntelegram = []\n",
        "[channels.telegram]\n[projects]\nbad = []\n",
    ],
)
def test_wrong_toml_container_types_are_domain_config_errors(tmp_path, body):
    _write(tmp_path, body)
    with pytest.raises(ValueError, match="expected a table"):
        cfgmod.load_config(tmp_path)


def test_deliver_only_telegram_needs_no_rig_and_no_projects(tmp_path):
    """Outbound-only delivery must not require an unused session target repository."""
    _write(tmp_path, '[channels.telegram]\n')
    c = cfgmod.load_config(tmp_path)
    assert c.telegram.rig == "" and c.telegram.projects == {} and c.telegram.default == ""
    assert c.telegram.token_env == "TELEGRAM_BOT_TOKEN"


def test_multi_without_default_is_allowed_but_empty(tmp_path):
    rig = fake_rig(tmp_path)
    (tmp_path / "p1").mkdir()
    (tmp_path / "p2").mkdir()
    _write(tmp_path, f'''
[channels.telegram]
rig = "{rig}"
[projects.one]
path = "{tmp_path / 'p1'}"
[projects.two]
path = "{tmp_path / 'p2'}"
''')
    assert cfgmod.load_config(tmp_path).telegram.default == ""


def test_telegram_project_can_select_configured_event_target_without_rig(tmp_path):
    _write(tmp_path, '''
[channels.telegram]

[targets.runtime]
argv = ["python3", "target.py"]

[projects.events]
target = "runtime"
''')
    config = cfgmod.load_config(tmp_path)
    assert config.telegram.rig == ""
    assert config.telegram.default == "events"
    assert config.telegram.projects["events"] == cfgmod.Project(target="runtime")

    _write(tmp_path, '''
[channels.telegram]
[projects.events]
target = "missing"
''')
    with pytest.raises(ValueError, match="is not declared"):
        cfgmod.load_config(tmp_path)


def test_linear_stub_parses_next_to_working_channel(tmp_path):
    rig = fake_rig(tmp_path)
    (tmp_path / "p").mkdir()
    _write(tmp_path, f'''
[channels.telegram]
rig = "{rig}"
[projects.a]
path = "{tmp_path / 'p'}"
[channels.linear]
token_env = "MY_LINEAR"
workspace = "acme"
''')
    c = cfgmod.load_config(tmp_path)
    assert c.linear.token_env == "MY_LINEAR" and c.linear.workspace == "acme"


def test_resolve_webhook_token_fail_closed():
    cfg = cfgmod.WebhookCfg(host="127.0.0.1", port=0, token_env="GW_T", routes={})
    with pytest.raises(ValueError, match="GW_T"):
        cfgmod.resolve_webhook_token(cfg, env={})                  # Missing value.
    with pytest.raises(ValueError, match="16"):
        cfgmod.resolve_webhook_token(cfg, env={"GW_T": "short"})
    assert cfgmod.resolve_webhook_token(cfg, env={"GW_T": "x" * 16}) == "x" * 16


def test_load_env_process_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# credentials\nexport A_TOKEN="synthetic-value"\nB=1\nINVALID_LINE\nC=\'x\'\n', "utf-8")
    monkeypatch.delenv("A_TOKEN", raising=False)
    monkeypatch.delenv("C", raising=False)
    monkeypatch.setenv("B", "already-set")         # Process environment wins.
    n = cfgmod.load_env(str(env))
    try:
        assert n == 2 and os.environ["A_TOKEN"] == "synthetic-value" and os.environ["B"] == "already-set"
        assert os.environ["C"] == "x"
    finally:
        os.environ.pop("A_TOKEN", None)
        os.environ.pop("C", None)
    assert cfgmod.load_env(str(tmp_path / "missing")) == 0  # Missing file is not fatal.
