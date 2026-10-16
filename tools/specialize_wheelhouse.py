"""Pre-evaluate Requires-Dist markers for the fixed target (linux aarch64
cp311 CPython, extras: httpx[socks]) and strip them from wheel METADATA,
keeping only active requirements without markers. The App Center builder's
marker grammar is intentionally narrow; the device never re-resolves.
"""
import base64, hashlib, os, zipfile
from packaging.requirements import Requirement

TARGET = {
    "python_version": "3.11", "python_full_version": "3.11.6",
    "platform_machine": "aarch64", "sys_platform": "linux",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux", "os_name": "posix",
    "implementation_name": "cpython",
}
EXTRAS = {"httpx": {"socks"}}


def rec_hash(data):
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(data).digest()).rstrip(b"=").decode()


def specialize(path):
    name = os.path.basename(path).split("-")[0].lower().replace("_", "-")
    extras = EXTRAS.get(name, set())
    with zipfile.ZipFile(path) as z:
        members = z.namelist()
        contents = {m: z.read(m) for m in members}
    meta_m = [m for m in members if m.endswith(".dist-info/METADATA")][0]
    record_m = [m for m in members if m.endswith(".dist-info/RECORD")][0]
    lines = contents[meta_m].decode().splitlines(keepends=True)
    out_lines, dropped, specialized = [], [], False
    for line in lines:
        if line.startswith("Requires-Dist:"):
            req = Requirement(line[len("Requires-Dist:"):].strip())
            if req.marker is None:
                out_lines.append(line)
                continue
            active = False
            for extra in ([""] + sorted(extras)):
                try:
                    if req.marker.evaluate(dict(TARGET, extra=extra)):
                        active = True
                except Exception:
                    active = True  # unknown marker -> keep (fail open)
            if active:
                specialized = True
                out_lines.append(f"Requires-Dist: {req.name}{req.specifier}\n")
            else:
                dropped.append(str(req))
        else:
            out_lines.append(line)
    if not dropped and not specialized:
        return None
    contents[meta_m] = "".join(out_lines).encode()
    record_lines = []
    for line in contents[record_m].decode().splitlines():
        member = line.split(",", 1)[0]
        if member == meta_m:
            data = contents[meta_m]
            record_lines.append(f"{meta_m},{rec_hash(data)},{len(data)}")
        else:
            record_lines.append(line)
    contents[record_m] = ("\n".join(record_lines) + "\n").encode()
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for m in members:
            out.writestr(m, contents[m])
    os.replace(tmp, path)
    return dropped, specialized


def main():
    for entry in sorted(os.listdir("wheelhouse")):
        if not entry.endswith(".whl"):
            continue
        r = specialize(os.path.join("wheelhouse", entry))
        if r:
            dropped, specialized = r
            print(f"{entry}: dropped={len(dropped)} specialized={specialized}")


if __name__ == "__main__":
    main()

import re

def normalize_specifiers(path):
    """==1.* -> >=1,<2 ; drop !=x.y.* atoms (pre-verified against this
    closure). Builder grammar is numeric-only."""
    with zipfile.ZipFile(path) as z:
        members = z.namelist()
        contents = {m: z.read(m) for m in members}
    meta_m = [m for m in members if m.endswith(".dist-info/METADATA")][0]
    record_m = [m for m in members if m.endswith(".dist-info/RECORD")][0]
    lines = contents[meta_m].decode().splitlines(keepends=True)
    changed = False
    out_lines = []
    for line in lines:
        if not line.startswith("Requires-Dist:") or ".*" not in line:
            out_lines.append(line)
            continue
        body = line[len("Requires-Dist:"):].strip()
        req = Requirement(body)
        atoms = []
        for atom in str(req.specifier).split(","):
            atom = atom.strip()
            m = re.fullmatch(r"==(\d+(?:\.\d+)*)\.\*", atom)
            if m:
                base = m.group(1)
                upper = base.split(".")
                upper[0] = str(int(upper[0]) + 1)
                atoms += [f">={base}", f"<{'.'.join(upper[:1]) if len(upper)==1 else '.'.join(upper)}"]
                changed = True
                continue
            if re.fullmatch(r"!=\d+(?:\.\d+)*\.\*", atom):
                changed = True
                continue  # exclusion wildcard: satisfied by the pinned closure
            atoms.append(atom)
        out_lines.append(f"Requires-Dist: {req.name}{','.join(atoms)}\n")
    if not changed:
        return False
    contents[meta_m] = "".join(out_lines).encode()
    record_lines = []
    for line in contents[record_m].decode().splitlines():
        member = line.split(",", 1)[0]
        if member == meta_m:
            data = contents[meta_m]
            record_lines.append(f"{meta_m},{rec_hash(data)},{len(data)}")
        else:
            record_lines.append(line)
    contents[record_m] = ("\n".join(record_lines) + "\n").encode()
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for m in members:
            out.writestr(m, contents[m])
    os.replace(tmp, path)
    return True


def main_all():
    for entry in sorted(os.listdir("wheelhouse")):
        if entry.endswith(".whl"):
            if normalize_specifiers(os.path.join("wheelhouse", entry)):
                print("normalized specifiers:", entry)


main_all()
