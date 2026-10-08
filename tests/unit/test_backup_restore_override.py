import re
from pathlib import Path

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
