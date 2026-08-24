from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from zipfile import ZipFile


def finalize_package(package_path: Path) -> None:
    package_path = package_path.resolve()
    temporary_path = package_path.with_name(f"{package_path.name}.tmp")

    with ZipFile(package_path) as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]

    manifest_entry = next(
        ((info, content) for info, content in entries if info.filename == "manifest.json"),
        None,
    )
    if manifest_entry is None:
        raise ValueError("Teams package does not contain manifest.json")

    manifest = json.loads(manifest_entry[1])
    bots = manifest.get("bots")
    if not isinstance(bots, list) or not bots:
        raise ValueError("Teams package does not declare a bot")

    manifest.pop("copilotAgents", None)
    for bot in bots:
        bot["scopes"] = ["personal"]

    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    try:
        with ZipFile(temporary_path, "w") as destination:
            for info, content in entries:
                destination.writestr(
                    info,
                    manifest_bytes if info.filename == "manifest.json" else content,
                )
        os.replace(temporary_path, package_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keep only the personal Teams bot in a generated app package."
    )
    parser.add_argument("package", type=Path)
    args = parser.parse_args()

    finalize_package(args.package)
    print(f"Finalized personal Teams package: {args.package}")


if __name__ == "__main__":
    main()
