#!/usr/bin/env python3
"""Materialize recamera-local and build its wheel from the pinned source.

Only runtime skill files are shipped: tests, examples, caches and observer-specific
experiments stay out of the Nanobot package. The wheel is regenerated from the
same source tree, never edited in place.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "lib" / "reCamera-Intellisense" / "skills" / "recamera-local"
DESTINATION = ROOT / "skills" / "recamera-local"
RUNTIME_DOCS = ("SKILL.md", "REFERENCE.md", "OBSERVATION.md")


def _source_commit() -> tuple[str, int]:
    git = ("git", "-C", str(SOURCE))
    commit = subprocess.check_output((*git, "rev-parse", "HEAD"), text=True).strip()
    timestamp = int(subprocess.check_output(
        (*git, "show", "-s", "--format=%ct", "HEAD"), text=True,
    ).strip())
    return commit, timestamp


def _runtime_files() -> list[Path]:
    files = [SOURCE / name for name in RUNTIME_DOCS]
    files.extend(sorted((SOURCE / "scripts" / "recamera_local").glob("*.py")))
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        raise RuntimeError(f"recamera-local source files missing: {missing}")
    if not (SOURCE / "OBSERVATION.md").is_file():
        raise RuntimeError("the pinned Intellisense source lacks OBSERVATION.md")
    return files


def _materialize(files: list[Path]) -> None:
    import shutil

    if DESTINATION.exists():
        shutil.rmtree(DESTINATION)
    for source in files:
        relative = source.relative_to(SOURCE)
        destination = DESTINATION / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _entry(archive: zipfile.ZipFile, name: str, data: bytes, timestamp: int) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    # Keep the source commit's timestamp in the archive comment, not in each member:
    # fixed member times plus sorted names make repeated builds byte-identical.
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def _build_wheel(files: list[Path], version: str, commit: str, timestamp: int,
                 wheelhouse: Path) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    destination = wheelhouse / f"recamera_local-{version}-py3-none-any.whl"
    for old in wheelhouse.glob("recamera_local-*-py3-none-any.whl"):
        if old != destination:
            old.unlink()
    dist_info = f"recamera_local-{version}.dist-info"
    payload: dict[str, bytes] = {}
    for source in files:
        if source.parent.name != "recamera_local":
            continue  # docs belong to the skill, not the import wheel
        payload[f"recamera_local/{source.name}"] = source.read_bytes()
    payload[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.1\n"
        "Name: recamera-local\n"
        f"Version: {version}\n"
        "Summary: reCamera local API helper bundled with the recamera-local nanobot skill\n"
    ).encode()
    payload[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\n"
        "Generator: sync_recamera_local.py\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode()
    payload[f"{dist_info}/top_level.txt"] = b"recamera_local\n"
    record_name = f"{dist_info}/RECORD"
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, data in sorted(payload.items()):
        writer.writerow((name, _record_hash(data), len(data)))
    writer.writerow((record_name, "", ""))
    payload[record_name] = output.getvalue().encode()
    with zipfile.ZipFile(destination, "w") as archive:
        for name, data in sorted(payload.items()):
            _entry(archive, name, data, timestamp)
        archive.comment = f"reCamera-Intellisense {commit}\n".encode()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="1.2.0")
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / "build" / "wheelhouse")
    args = parser.parse_args()
    commit, timestamp = _source_commit()
    files = _runtime_files()
    _materialize(files)
    wheel = _build_wheel(files, args.version, commit, timestamp, args.wheelhouse)
    print(f"recamera-local source: {commit}")
    print(f"materialized {len(files)} runtime files under {DESTINATION}")
    print(f"built {wheel} ({wheel.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
