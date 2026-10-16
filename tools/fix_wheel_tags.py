#!/usr/bin/env python3
"""Normalize wheel WHEEL Tag lines to the single compressed filename tag.

The App Center packaging contract declares tags == [filename tag]; PyPI
wheels with compressed multi-platform filenames list the expanded tags in
WHEEL instead. The device installer never parses WHEEL tags (structure,
ELF arch and RECORD presence only), so rewriting the Tag lines — and
re-hashing RECORD accordingly — is safe and keeps the builder honest.

Usage: fix_wheel_tags.py <wheelhouse-dir>
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import zipfile


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def fix_wheel(path: str) -> bool:
    name = os.path.basename(path)
    parts = name[:-4].split("-")
    if len(parts) != 5:
        return False
    filename_tag = "-".join(parts[2:])
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        wheel_members = [m for m in members if m.endswith(".dist-info/WHEEL")]
        record_members = [m for m in members if m.endswith(".dist-info/RECORD")]
        if len(wheel_members) != 1 or len(record_members) != 1:
            return False
        wheel_name, record_name = wheel_members[0], record_members[0]
        wheel_text = archive.read(wheel_name).decode("utf-8")
        tags = [line[4:].strip() for line in wheel_text.splitlines()
                if line.startswith("Tag:")]
        if tags == [filename_tag]:
            return False
        # safe to rewrite when the py+abi pair matches; the platform tail is
        # exactly what renames to linux_aarch64 change
        fp = filename_tag.split("-")[:2]
        if filename_tag not in tags and not any(
                t.split("-")[:2] == fp for t in tags):
            return False  # unrelated layout; leave untouched
        contents = {m: archive.read(m) for m in members}

    lines = [line for line in wheel_text.splitlines()
             if not line.startswith("Tag:")]
    lines.append(f"Tag: {filename_tag}")
    new_wheel = "\n".join(lines) + "\n"
    contents[wheel_name] = new_wheel.encode("utf-8")

    record_lines = []
    for line in contents[record_name].decode("utf-8").splitlines():
        member = line.split(",", 1)[0]
        if member == wheel_name:
            data = contents[wheel_name]
            record_lines.append(
                f"{wheel_name},{_record_hash(data)},{len(data)}")
        else:
            record_lines.append(line)
    contents[record_name] = ("\n".join(record_lines) + "\n").encode("utf-8")

    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for member in members:
            out.writestr(member, contents[member])
    os.replace(tmp, path)
    return True


def main() -> int:
    root = sys.argv[1]
    fixed = []
    for entry in sorted(os.listdir(root)):
        if entry.endswith(".whl"):
            if fix_wheel(os.path.join(root, entry)):
                fixed.append(entry)
    print(f"normalized {len(fixed)} wheels:")
    for entry in fixed:
        print(" ", entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
