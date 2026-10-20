"""Settings parsing rules that security depends on."""
from config.settings import AppSettings


def test_cors_origins_parse_and_strip(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", " https://a.example , https://b.example ")
    s = AppSettings(_env_file=None)
    assert s.cors_origins() == ["https://a.example", "https://b.example"]


def test_cors_wildcard_disables_credentials(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
    s = AppSettings(_env_file=None)
    assert s.cors_origins() == ["*"] and s.cors_allow_credentials() is False


def test_cors_default_is_local_only(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    s = AppSettings(_env_file=None)
    assert s.cors_origins() == ["http://localhost:8000"] and s.cors_allow_credentials() is True
