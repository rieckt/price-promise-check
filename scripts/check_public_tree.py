#!/usr/bin/env python3
"""Heuristic privacy check for Git-visible source and built distributions."""
import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PATTERNS = {
    "personal_absolute_path": re.compile(r"(?:/Users/|/home/)[A-Za-z0-9_.-]+/|[A-Z]:\\Users\\[^\\\s]+\\"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_pattern": re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b"),
}
PRIVATE_DIRS = {"private", "exports", "logs", ".ssh", ".config", ".venv"}
PRIVATE_NAMES = {"config.json", "config.local.json", "credentials.json"}


def private_literals():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
    path = base / "pricecheck" / "config.json"
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        private = value.get("home_address", {})
        if not isinstance(private, dict):
            raise ValueError
        return [v.casefold() for key, v in private.items()
                if key in {"street", "full_address", "email", "phone", "name"}
                and isinstance(v, str) and len(v) >= 6]
    except (OSError, ValueError, AttributeError, UnicodeError):
        raise ValueError("Private configuration cannot be read for the privacy check") from None


def inspect_content(name, content, literals):
    path = PurePosixPath(name)
    findings = []
    if (path.is_absolute() or ".." in path.parts or set(path.parts) & PRIVATE_DIRS
            or path.name in PRIVATE_NAMES or path.name.startswith(".env")
            or path.suffix in {".pem", ".key"}):
        findings.append("private_file_or_unsafe_path")
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return findings  # Binary files require separate review.
    findings.extend(kind for kind, pattern in PATTERNS.items() if pattern.search(text))
    if any(value in text.casefold() for value in literals):
        findings.append("private_configuration_value")
    if path.suffix == ".json":
        try:
            value = json.loads(text)
            if isinstance(value, dict) and value.get("home_address"):
                findings.append("private_address_object")
        except ValueError:
            findings.append("invalid_json")
    return findings


def source_members(root):
    result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=root, capture_output=True, check=True)
    for name in sorted(set(result.stdout.decode("utf-8").split("\0")) - {""}):
        path = root / name
        if path.is_symlink():
            yield name, b"", ["source_symlink_requires_review"]
        else:
            yield name, path.read_bytes(), []


def artifact_members(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info), []
    else:
        with tarfile.open(path, "r:*") as archive:
            for info in archive:
                if info.issym() or info.islnk():
                    yield info.name, b"", ["archive_link_requires_review"]
                elif info.isfile():
                    stream = archive.extractfile(info)
                    with stream:
                        yield info.name, stream.read(), []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, help="Inspect a wheel or source archive without extracting it")
    args = parser.parse_args(argv)
    try:
        literals = private_literals()
        members = artifact_members(args.artifact) if args.artifact else source_members(Path(__file__).resolve().parents[1])
        count = 0
        failures = []
        for name, content, extra in members:
            count += 1
            kinds = extra + inspect_content(name, content, literals)
            if kinds:
                failures.append({"file": name, "findings": kinds})
        print(json.dumps({"status": "failed" if failures else "ok", "files_checked": count,
                          "findings": failures}, ensure_ascii=False))
        return 1 if failures else 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile):
        print(json.dumps({"status": "failed", "error": "Privacy check could not inspect its inputs"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
