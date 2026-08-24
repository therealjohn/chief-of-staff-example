from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


_SCRIPT_PATH = (
    Path(__file__).parents[1] / "scripts" / "finalize_teams_package.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "finalize_teams_package",
        _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalize_package_keeps_only_the_personal_teams_bot(tmp_path):
    package_path = tmp_path / "appPackage.zip"
    manifest = {
        "id": "app-id",
        "version": "1.0.2",
        "name": {"short": "Chief of Staff", "full": "Chief of Staff"},
        "bots": [
            {
                "botId": "bot-id",
                "scopes": ["personal", "team", "groupChat", "copilot"],
            }
        ],
        "copilotAgents": {
            "customEngineAgents": [
                {"id": "bot-id", "type": "bot"},
            ]
        },
    }
    with ZipFile(package_path, "w", ZIP_DEFLATED) as package:
        package.writestr("manifest.json", json.dumps(manifest))
        package.writestr("color.png", b"color-icon")
        package.writestr("outline.png", b"outline-icon")

    script = _load_script()
    script.finalize_package(package_path)

    with ZipFile(package_path) as package:
        finalized = json.loads(package.read("manifest.json"))
        assert "copilotAgents" not in finalized
        assert finalized["bots"][0]["scopes"] == ["personal"]
        assert package.read("color.png") == b"color-icon"
        assert package.read("outline.png") == b"outline-icon"
