import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture(autouse=True)
def forbid_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Tests must inject a command runner; real commands are forbidden")

    monkeypatch.setattr("subprocess.run", forbidden)


@pytest.fixture
def host(monkeypatch):
    def configure(system="Darwin", machine="arm64", brew="/opt/homebrew/bin/brew"):
        monkeypatch.setattr("platform.system", lambda: system)
        monkeypatch.setattr("platform.machine", lambda: machine)
        monkeypatch.setattr("shutil.which", lambda name: brew if name == "brew" else None)
        monkeypatch.setattr("getpass.getuser", lambda: "alice")
        monkeypatch.setattr("os.getgid", lambda: 20)
        monkeypatch.setattr("grp.getgrgid", lambda gid: type("Group", (), {"gr_name": "staff"})())

    configure()
    return configure


@pytest.fixture
def fake_runner():
    from sites_platform import CommandResult

    class Runner:
        def __init__(self, responses):
            self.responses = responses
            self.calls = []

        def __call__(self, command):
            key = tuple(command)
            self.calls.append(key)
            if key not in self.responses:
                pytest.fail(f"Unexpected command: {key}")
            response = self.responses[key]
            return response if isinstance(response, CommandResult) else CommandResult(response, "", 0)

    return Runner
