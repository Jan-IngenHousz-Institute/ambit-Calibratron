"""Regression checks for stale asynchronous GUI sign-in completions."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import calibratron_gui as gui


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Settings:
    def __init__(self):
        self.environment = "dev"
        self.changes = []
        self.saves = 0

    def set_api_key(self, environment, key):
        self.changes.append((environment, key))

    def save(self):
        self.saves += 1


@pytest.mark.parametrize(
    "current_generation,current_environment,result_generation,result_environment",
    [(5, "dev", 4, "dev"), (5, "dev", 5, "prod")],
    ids=["older-request", "environment-changed"],
)
def test_stale_signed_in_result_does_not_replace_current_auth_state(
        current_generation, current_environment, result_generation,
        result_environment):
    app = gui.CalibratronGUI.__new__(gui.CalibratronGUI)
    app._auth_generation = current_generation
    app.oj_env = Value(current_environment)
    app.settings = Settings()
    app.oj_user = Value("current authentication state")
    existing_client = object()
    app.oj_client = existing_client
    client = SimpleNamespace(env=SimpleNamespace(key=result_environment),
                             who=lambda: "stale-user")

    app._show_signed_in((result_generation, client, "jii_stale"))

    assert app.oj_client is existing_client
    assert app.oj_user.get() == "current authentication state"
    assert app.settings.changes == []
    assert app.settings.saves == 0
    assert app.settings.environment == "dev"
