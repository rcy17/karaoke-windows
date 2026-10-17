"""Legacy API behavior is tested explicitly; production defaults are tested separately."""
import pytest


@pytest.fixture(autouse=True)
def explicit_test_backends(monkeypatch):
    monkeypatch.setenv('KARAOKE_ASR_BACKEND', 'api')
    monkeypatch.setenv('KARAOKE_TEXT_BACKEND', 'api')
