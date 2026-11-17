import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "ensure_env", Path(__file__).parents[2] / "scripts" / "ensure_env.py"
)
ensure_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ensure_env)


def test_fresh_env_is_consistent_and_idempotent(tmp_path):
    path = tmp_path / ".env"
    ensure_env.ensure(path)
    first = path.read_text()
    values = ensure_env._parse(first)
    assert values["DATABASE_URL"].split(":", 2)[2].startswith(values["POSTGRES_PASSWORD"] + "@")
    assert values["REDIS_URL"] == f"redis://:{values['REDISCLI_AUTH']}@redis:6379/0"
    assert values["NEO4J_AUTH"] == f"neo4j/{values['GRAPH_PASSWORD']}"
    assert len(values["AI_CREDENTIAL_ENCRYPTION_KEY"]) == 44
    ensure_env.ensure(path)
    assert path.read_text() == first


def test_existing_values_kept_and_placeholders_filled(tmp_path):
    path = tmp_path / ".env"
    path.write_text("POSTGRES_PASSWORD=mine\nREDISCLI_AUTH=replace-with-a-redis-password\n")
    ensure_env.ensure(path)
    values = ensure_env._parse(path.read_text())
    assert values["POSTGRES_PASSWORD"] == "mine"
    assert "mine@postgres" in values["DATABASE_URL"]
    assert not values["REDISCLI_AUTH"].startswith("replace-")
    assert values["REDIS_URL"].startswith(f"redis://:{values['REDISCLI_AUTH']}@")


def test_env_example_loads_in_production_with_local_storage(tmp_path, monkeypatch):
    """The shipped example plus ensure_env must not trip the placeholder check for the default local backend."""
    from core.config import Settings

    example = Path(__file__).parents[2] / ".env.example"
    path = tmp_path / ".env"
    path.write_text(example.read_text())
    ensure_env.ensure(path)
    values = ensure_env._parse(path.read_text())
    for key in list(__import__("os").environ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://umwelt.example.com")
    monkeypatch.setenv("SECURE_COOKIES", "true")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    assert Settings(_env_file=None).storage_backend == "local"
