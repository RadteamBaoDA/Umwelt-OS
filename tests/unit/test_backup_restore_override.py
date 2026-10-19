import re
from pathlib import Path
from types import SimpleNamespace

from modules.backup import host


def test_restore_override_covers_every_started_service(tmp_path: Path) -> None:
    override = tmp_path / "override.yml"
    host._write_restore_override(override, tmp_path / "archived.env")
    text = override.read_text(encoding="utf-8")
    overridden = set(re.findall(r"^  ([\w-]+):\n    env_file: !override", text, re.MULTILINE))
    source = Path(host.__file__).read_text(encoding="utf-8")
    started = {
        name
        for call in re.findall(r'quiet\("up", "-d", ([^)]*)\)', source)
        for name in re.findall(r'"([\w-]+)"', call)
    } - {"postgres", "redis", "graph"}  # image-only services without the application .env
    assert {"api", "worker", "chat-worker"} <= started
    assert (started | {"migrate"}) <= overridden


class _FakeCompose:
    """Compose stand-in that replays scripted capture outputs."""

    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, ...]] = []

    def quiet(self, *args: str) -> None:
        self.calls.append(args)

    def capture(self, *args: str) -> bytes:
        self.calls.append(args)
        return self.outputs.pop(0)


def test_verify_graph_volume_requires_online_neo4j(monkeypatch) -> None:
    monkeypatch.setattr(host.time, "sleep", lambda _s: None)
    compose = _FakeCompose([b"currentStatus\n\"offline\"\n", b"currentStatus\nonline\n"])
    host._verify_graph_volume(compose)  # type: ignore[arg-type]
    assert "cypher-shell" in compose.calls[1][-1]


def test_verify_graph_volume_times_out(monkeypatch) -> None:
    ticks = iter([0.0, 1.0, 1000.0])
    monkeypatch.setattr(host.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(host.time, "sleep", lambda _s: None)
    try:
        host._verify_graph_volume(_FakeCompose([b"offline"]))  # type: ignore[arg-type]
    except host.BackupHostError as exc:
        assert "pinned Neo4j runtime" in str(exc)
    else:
        raise AssertionError("expected BackupHostError")


def test_legacy_graph_format_is_rejected() -> None:
    legacy = SimpleNamespace(status="complete", schema_version="falkordb-rdb-volume-v1")
    current = SimpleNamespace(status="complete", schema_version=host.GRAPH_SCHEMA_VERSION)
    host._require_graph_format(current)
    host._require_graph_format(None)
    try:
        host._require_graph_format(legacy)
    except host.BackupHostError as exc:
        assert "incompatible_format" in str(exc)
    else:
        raise AssertionError("expected BackupHostError")
