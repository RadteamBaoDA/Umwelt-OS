"""Mirror the project skills from ``.claude/skills`` (source of truth) into ``.agents/skills``.

Claude Code reads ``.claude/skills``; other agents read ``.agents/skills``. Edit skills only under ``.claude``,
then run ``python scripts/sync_skills.py``. ``--check`` changes nothing and exits 1 when the trees differ (CI).
"""

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / ".claude" / "skills"
TARGET = ROOT / ".agents" / "skills"


def drift() -> list[str]:
    """Return relative paths that are missing, extra or different in the target tree."""
    problems: list[str] = []

    def walk(cmp: filecmp.dircmp[str], prefix: str) -> None:
        """Collect differences of one directory pair and recurse into common subdirectories."""
        problems.extend(f"missing {prefix}{n}" for n in cmp.left_only)
        problems.extend(f"extra {prefix}{n}" for n in cmp.right_only)
        problems.extend(f"differs {prefix}{n}" for n in cmp.diff_files)
        for name, sub in cmp.subdirs.items():
            walk(sub, f"{prefix}{name}/")

    walk(filecmp.dircmp(SOURCE, TARGET, ignore=["__pycache__"]), "")
    return problems


def main() -> int:
    """Check or rewrite the mirror; returns the process exit status."""
    if "--check" in sys.argv[1:]:
        problems = drift()
        for problem in problems:
            print(problem)
        if problems:
            print("Skill trees differ; run: python scripts/sync_skills.py")
        return 1 if problems else 0
    shutil.rmtree(TARGET, ignore_errors=True)
    shutil.copytree(SOURCE, TARGET, ignore=shutil.ignore_patterns("__pycache__"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
