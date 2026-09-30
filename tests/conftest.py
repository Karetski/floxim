import json
import os
from pathlib import Path
from typing import Any

import pytest

GOLDEN = Path(__file__).parent / "golden"


class Golden:
    """Compare a JSON-serializable value with a checked-in golden file.

    Run with FLOXIM_UPDATE_GOLDEN=1 to rewrite the files, then review the diff.
    """

    def check(self, name: str, value: Any) -> None:
        path = GOLDEN / name
        text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if os.environ.get("FLOXIM_UPDATE_GOLDEN"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        assert path.exists(), f"missing golden file {path}; run with FLOXIM_UPDATE_GOLDEN=1"
        assert json.loads(path.read_text()) == json.loads(text), f"{name} differs from golden"


@pytest.fixture
def golden() -> Golden:
    return Golden()


@pytest.fixture
def write_flow(tmp_path: Path) -> Any:
    """Write files into a temporary project and return the path of the first."""

    def write(files: dict[str, str]) -> Path:
        paths = []
        for name, text in files.items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            paths.append(path)
        return paths[0]

    return write


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never read the developer's own ~/.config/floxim in tests."""
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home / "floxim" / "config.yaml"
