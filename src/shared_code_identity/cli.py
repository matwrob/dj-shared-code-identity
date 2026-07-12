#!/usr/bin/env python3
"""Verify byte-identical shared code defined by similarity manifests."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOTS = {
    "core": Path("core"),
    "veles": Path("veles"),
    "hub": Path("hub"),
    "mailman": Path("mailman"),
    "doccreator": Path("doccreator"),
}

# Case-insensitive, word-bounded match of a project's own name inside its own
# files. Word boundaries keep unrelated words intact (e.g. "github" never
# matches "hub") while still catching hyphenated forms like "core-django".
PROJECT_NAME_PATTERNS = {
    project: re.compile(rf"\b{re.escape(project)}\b", re.IGNORECASE)
    for project in PROJECT_ROOTS
}

# NUL bytes cannot occur in decodable UTF-8 text files, so these placeholders
# can never collide with real file content.
_PLACEHOLDER_LOWER = "\x00project\x00"
_PLACEHOLDER_TITLE = "\x00Project\x00"
_PLACEHOLDER_UPPER = "\x00PROJECT\x00"

ANCHORS = ("src", "root")


LIST_KEYS = {
    "applicable_projects",
    "src_paths",
    "src_excludes",
    "root_paths",
    "root_excludes",
    "project_name_insensitive",
}


DEFAULT_EXCLUDES = {
    "*/__pycache__/*",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".DS_Store",
    "*.swp",
    "*.bak",
    "~*",
}


@dataclass(frozen=True)
class Manifest:
    path: Path
    name: str
    description: str
    canonical_project: str
    applicable_projects: tuple[str, ...]
    src_paths: tuple[str, ...]
    src_excludes: tuple[str, ...]
    root_paths: tuple[str, ...]
    root_excludes: tuple[str, ...]
    project_name_insensitive: tuple[str, ...]


@dataclass(frozen=True)
class SharedFile:
    anchor: str
    relative_path: str


def parse_manifest(path: Path) -> Manifest:
    data: dict[str, str | list[str]] = {}
    current_key: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.startswith("  - "):
            if current_key is None:
                raise ValueError(f"{path}: list item without a list key: {raw_line}")
            value = line[4:].strip()
            if not isinstance(data[current_key], list):
                raise ValueError(f"{path}: key {current_key!r} is not a list")
            data[current_key].append(value)
            continue
        if line.startswith((" ", "\t")):
            raise ValueError(f"{path}: unsupported indentation: {raw_line}")
        if ":" not in line:
            raise ValueError(f"{path}: expected 'key: value': {raw_line}")

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in LIST_KEYS:
            data[key] = []
            current_key = key
            if value and value != "[]":
                raise ValueError(f"{path}: list key {key!r} must use '- item' lines")
        else:
            data[key] = value
            current_key = None

    required = {"name", "canonical_project", "applicable_projects"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    def scalar(key: str, default: str = "") -> str:
        value = data.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f"{path}: key {key!r} must be scalar")
        return value

    def list_value(key: str) -> tuple[str, ...]:
        value = data.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{path}: key {key!r} must be a list")
        return tuple(value)

    manifest = Manifest(
        path=path,
        name=scalar("name"),
        description=scalar("description"),
        canonical_project=scalar("canonical_project"),
        applicable_projects=list_value("applicable_projects"),
        src_paths=list_value("src_paths"),
        src_excludes=list_value("src_excludes"),
        root_paths=list_value("root_paths"),
        root_excludes=list_value("root_excludes"),
        project_name_insensitive=list_value("project_name_insensitive"),
    )
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: Manifest) -> None:
    projects = (manifest.canonical_project, *manifest.applicable_projects)
    unknown = sorted({project for project in projects if project not in PROJECT_ROOTS})
    if unknown:
        raise ValueError(
            f"{manifest.path}: unknown project(s): {', '.join(unknown)}"
        )
    if manifest.canonical_project not in manifest.applicable_projects:
        raise ValueError(
            f"{manifest.path}: canonical_project must be in applicable_projects"
        )
    bad_entries = [
        entry
        for entry in manifest.project_name_insensitive
        if entry.split(":", 1)[0] not in ANCHORS or ":" not in entry
    ]
    if bad_entries:
        raise ValueError(
            f"{manifest.path}: project_name_insensitive entries must be "
            f"'src:<pattern>' or 'root:<pattern>': {', '.join(bad_entries)}"
        )


def project_root(workspace: Path, project: str) -> Path:
    return workspace / PROJECT_ROOTS[project]


def src_root(workspace: Path, project: str) -> Path:
    return project_root(workspace, project) / "src" / f"{project}-django"


def expand_patterns(
    base: Path,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    files: set[str] = set()
    unmatched: list[str] = []
    combined_excludes = set(excludes) | DEFAULT_EXCLUDES

    for pattern in includes:
        matches = sorted(
            path.relative_to(base).as_posix()
            for path in base.glob(pattern)
            if path.is_file()
        )
        if not matches:
            unmatched.append(pattern)
        files.update(matches)

    for pattern in combined_excludes:
        files = {path for path in files if not fnmatch.fnmatchcase(path, pattern)}

    return sorted(files), unmatched


def manifest_files(workspace: Path, manifest: Manifest) -> tuple[list[SharedFile], list[str]]:
    canonical_src = src_root(workspace, manifest.canonical_project)
    canonical_repo = project_root(workspace, manifest.canonical_project)
    src_files, unmatched_src = expand_patterns(
        canonical_src, manifest.src_paths, manifest.src_excludes
    )
    root_files, unmatched_root = expand_patterns(
        canonical_repo, manifest.root_paths, manifest.root_excludes
    )

    files = [SharedFile("src", path) for path in src_files]
    files.extend(SharedFile("root", path) for path in root_files)
    unmatched = [f"src:{pattern}" for pattern in unmatched_src]
    unmatched.extend(f"root:{pattern}" for pattern in unmatched_root)
    return sorted(files, key=lambda item: (item.anchor, item.relative_path)), unmatched


def absolute_path(workspace: Path, project: str, shared_file: SharedFile) -> Path:
    if shared_file.anchor == "src":
        return src_root(workspace, project) / shared_file.relative_path
    return project_root(workspace, project) / shared_file.relative_path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _placeholder(token: str) -> str:
    if token.islower():
        return _PLACEHOLDER_LOWER
    if token.isupper():
        return _PLACEHOLDER_UPPER
    return _PLACEHOLDER_TITLE


def normalized_digest(path: Path, project: str) -> str:
    """Digest with the project's own name replaced by case-class placeholders.

    ``core`` -> lower, ``CORE`` -> upper, ``Core``/``DocCreator`` -> title, so
    files count as equivalent only when the casing style matches too. Files
    that are not valid UTF-8 fall back to a plain byte digest.
    """
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(data).hexdigest()
    normalized = PROJECT_NAME_PATTERNS[project].sub(
        lambda match: _placeholder(match.group(0)), text
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def flagged_files(
    manifest: Manifest, files: list[SharedFile]
) -> tuple[set[SharedFile], list[str]]:
    """Resolve project_name_insensitive patterns against tracked files."""
    flagged: set[SharedFile] = set()
    unmatched: list[str] = []
    for pattern in manifest.project_name_insensitive:
        hits = [
            shared_file
            for shared_file in files
            if fnmatch.fnmatchcase(
                f"{shared_file.anchor}:{shared_file.relative_path}", pattern
            )
        ]
        if hits:
            flagged.update(hits)
        else:
            unmatched.append(pattern)
    return flagged, unmatched


def raise_unmatched_patterns(
    workspace: Path, manifest: Manifest, unmatched: list[str]
) -> None:
    lines = []
    for item in unmatched:
        anchor, pattern = item.split(":", 1)
        if anchor == "src":
            base = src_root(workspace, manifest.canonical_project)
        else:
            base = project_root(workspace, manifest.canonical_project)
        lines.append(f"  {anchor}: {pattern!r} (searched {base})")
    raise ValueError(
        f"manifest {manifest.name!r}: {len(unmatched)} include pattern(s) "
        f"matched no files in canonical project "
        f"{manifest.canonical_project!r}:\n" + "\n".join(lines)
    )


def bucket(shared_file: SharedFile) -> str:
    path = shared_file.relative_path
    if shared_file.anchor == "root":
        return "repo"
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "apps":
        if len(parts) >= 4 and parts[1] == "tests":
            return f"tests/{parts[3]}"
        return parts[1]
    if parts:
        return parts[0]
    return "other"


def print_rows(title: str, rows: list[str], limit: int) -> None:
    if not rows:
        return
    print(title)
    for row in rows[:limit]:
        print(row)
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more")
    print()


def verify_manifest(workspace: Path, manifest: Manifest, detail_limit: int) -> tuple[int, int, int]:
    files, unmatched = manifest_files(workspace, manifest)
    if unmatched:
        raise_unmatched_patterns(workspace, manifest, unmatched)
    name_insensitive, unmatched_flags = flagged_files(manifest, files)
    if unmatched_flags:
        raise ValueError(
            f"manifest {manifest.name!r}: project_name_insensitive pattern(s) "
            f"matched no tracked files (patterns apply to files already "
            f"included via src_paths/root_paths): "
            + ", ".join(repr(item) for item in unmatched_flags)
        )

    total = len(files)
    identical = 0
    equivalent = 0
    differ = 0
    missing = 0
    by_bucket: dict[str, dict[str, int]] = {}
    drift_rows: list[str] = []
    equivalent_rows: list[str] = []
    missing_rows: list[str] = []

    for shared_file in files:
        label = bucket(shared_file)
        stats = by_bucket.setdefault(
            label,
            {"total": 0, "identical": 0, "equiv": 0, "differ": 0, "missing": 0},
        )
        stats["total"] += 1

        existing: dict[str, Path] = {}
        missing_projects: list[str] = []
        for project in manifest.applicable_projects:
            path = absolute_path(workspace, project, shared_file)
            if path.is_file():
                existing[project] = path
            else:
                missing_projects.append(project)

        if missing_projects:
            missing += 1
            stats["missing"] += 1
            missing_rows.append(
                f"  {shared_file.anchor}:{shared_file.relative_path} "
                f"(missing in: {', '.join(missing_projects)})"
            )
            continue

        digests = {project: digest(path) for project, path in existing.items()}
        if len(set(digests.values())) == 1:
            identical += 1
            stats["identical"] += 1
            continue

        if shared_file in name_insensitive:
            digests = {
                project: normalized_digest(path, project)
                for project, path in existing.items()
            }
            if len(set(digests.values())) == 1:
                equivalent += 1
                stats["equiv"] += 1
                equivalent_rows.append(
                    f"  {shared_file.anchor}:{shared_file.relative_path}"
                )
                continue

        differ += 1
        stats["differ"] += 1
        short = " ".join(
            f"{project}={digests[project][:8]}" for project in manifest.applicable_projects
        )
        note = " (differs beyond project name)" if shared_file in name_insensitive else ""
        drift_rows.append(
            f"  {short}  {shared_file.anchor}:{shared_file.relative_path}{note}"
        )

    print(f"=== {manifest.name} ===")
    print(manifest.description)
    print(f"canonical={manifest.canonical_project}")
    print(f"projects={', '.join(manifest.applicable_projects)}")
    print()
    print(
        f"{'Bucket':24} {'total':>6} {'ident':>6} {'equiv':>6} "
        f"{'differ':>6} {'miss':>6}"
    )
    print(f"{'-' * 24} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6}")
    for label in sorted(by_bucket):
        stats = by_bucket[label]
        print(
            f"{label:24} {stats['total']:6} {stats['identical']:6} "
            f"{stats['equiv']:6} {stats['differ']:6} {stats['missing']:6}"
        )
    print(f"{'-' * 24} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6}")
    print(
        f"{'TOTAL':24} {total:6} {identical:6} {equivalent:6} "
        f"{differ:6} {missing:6}"
    )
    print()

    print_rows("Equivalent (differ only by project name):", equivalent_rows, detail_limit)
    print_rows("Drift:", drift_rows, detail_limit)
    print_rows("Missing:", missing_rows, detail_limit)
    return differ, equivalent, missing


def load_manifests(manifest_dir: Path, selected: set[str] | None) -> list[Manifest]:
    manifests = [parse_manifest(path) for path in sorted(manifest_dir.glob("*.yaml"))]
    if selected:
        manifests = [
            manifest
            for manifest in manifests
            if manifest.name in selected or manifest.path.name in selected
        ]
        found = {manifest.name for manifest in manifests} | {manifest.path.name for manifest in manifests}
        unknown = sorted(selected - found)
        if unknown:
            raise ValueError(f"unknown manifest(s): {', '.join(unknown)}")
    return manifests


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    default_workspace = project_root.parent
    default_manifest_dir = project_root / "manifests"

    parser = argparse.ArgumentParser(
        description="Verify byte-identical shared code across Django projects."
    )
    parser.add_argument("--workspace", type=Path, default=default_workspace)
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=default_manifest_dir,
    )
    parser.add_argument("--manifest", action="append", help="Manifest name or filename to verify.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on drift or missing files.")
    parser.add_argument("--detail-limit", type=int, default=50, help="Maximum detailed rows per section.")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    manifest_dir = args.manifest_dir.resolve()
    selected = set(args.manifest or []) or None

    try:
        manifests = load_manifests(manifest_dir, selected)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not manifests:
        print(f"ERROR: no manifests found in {manifest_dir}", file=sys.stderr)
        return 2

    total_differ = 0
    total_equivalent = 0
    total_missing = 0
    manifest_errors = 0
    for index, manifest in enumerate(manifests):
        if index:
            print()
        try:
            differ, equivalent, missing = verify_manifest(
                workspace, manifest, args.detail_limit
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            manifest_errors += 1
            continue
        total_differ += differ
        total_equivalent += equivalent
        total_missing += missing

    print("=== Summary ===")
    print(
        f"manifests={len(manifests)} errors={manifest_errors} "
        f"differ={total_differ} equiv={total_equivalent} "
        f"missing={total_missing}"
    )

    if manifest_errors:
        return 2
    if args.strict and (total_differ or total_missing):
        print("FAIL: shared-code drift detected.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
