import pytest

from jarvis.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Unit tests never touch the real database or Ollama, whatever .env says."""
    monkeypatch.setenv("JARVIS_DATABASE_URL", "")
    monkeypatch.setenv("JARVIS_MCP_CONFIG", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
