"""Test builders kept outside conftest to make their use explicit.

Tests are hermetic: subprocess and HTTP are injected, with no network or paid
service access.
"""
from tool_gateway.core.channel import Channel
from tool_gateway.core.config import Project, TelegramCfg


def fake_rig(tmp_path) -> str:
    """Create a directory accepted by the rig repository validator."""
    (tmp_path / "rig" / "rig").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rig" / "rig" / "__main__.py").write_text("", "utf-8")
    return str(tmp_path / "rig")


def tcfg(tmp_path, projects=("alpha",), default="") -> TelegramCfg:
    """Build Telegram config with a fake target and project directories."""
    rig = fake_rig(tmp_path)
    reg = {}
    for name in projects:
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        reg[name] = Project(path=str(d))
    return TelegramCfg(rig=rig,
                       default=default or (projects[0] if len(projects) == 1 else ""),
                       projects=reg,
                       token_env="TELEGRAM_BOT_TOKEN", chat_id_env="TELEGRAM_CHAT_ID")


class Run:
    """A subprocess.run-compatible stub that records argv and returns a preset result."""

    def __init__(self, rc=0, out="READY", err=""):
        self.calls = []
        self.rc, self.out, self.err = rc, out, err

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))

        class R:  # noqa: N801 - minimal subprocess result
            returncode, stdout, stderr = self.rc, self.out, self.err
        return R()


class FakeChannel(Channel):
    """Channel stub whose first poll returns inbox and whose sends collect in outbox."""
    name = "fake"

    def __init__(self, inbox):
        self.inbox, self.outbox = list(inbox), []

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def send(self, text):
        self.outbox.append(text)
        return True
