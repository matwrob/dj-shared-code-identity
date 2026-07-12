# Shared Code Identity

Self-contained verifier for hierarchical similarity manifests that define
byte-identical code shared by related projects.

The default workspace layout expects this project to sit beside the five Django
projects:

- `core`
- `veles`
- `hub`
- `mailman`
- `doccreator`

## Usage

Run directly from the project directory:

```bash
PYTHONPATH=src python -m shared_code_identity.cli
PYTHONPATH=src python -m shared_code_identity.cli --strict
PYTHONPATH=src python -m shared_code_identity.cli --manifest jinja-rendering --strict
```

Or install the app in editable mode and use the console command:

```bash
python -m pip install -e .
verify-shared-code --strict
```

The verifier expands globs against each manifest's `canonical_project`, then
checks that every resolved file exists and has identical bytes in every
`applicable_projects` project.

An include pattern (`src_paths`/`root_paths` entry) that matches no files in
the canonical project is a hard error (exit code 2), independent of
`--strict`: it always means a stale path or a typo in the manifest.

## Manifest Format

Each manifest is YAML using a small, dependency-free subset:

```yaml
name: base
description: Code shared by all five Django projects.
canonical_project: hub
applicable_projects:
  - core
  - veles
  - hub
  - mailman
  - doccreator
src_paths:
  - apps/authentication/**/*.py
src_excludes:
  - apps/*/migrations/**
root_paths:
  - dc.sh
root_excludes: []
project_name_insensitive:
  - root:dc.sh
```

`src_paths` are resolved relative to `<project>/src/<project>-django/`.
`root_paths` are resolved relative to the project repository root. Exclusions
are applied after inclusions and support the same glob syntax.

Overlapping manifests are valid. A file can be governed by `base.yaml` and by a
more specific manifest; each manifest reports its own byte-identity result.

## Project-Name-Insensitive Files

Some shared files can never be byte-identical because they legitimately embed
the project's own name (e.g. `.pre-commit-config.yaml` hook commands
referencing `<project>-django`). List them under `project_name_insensitive`
as `src:<pattern>` / `root:<pattern>` glob patterns over files already
included via `src_paths`/`root_paths` (a pattern flagging no tracked file is
an error).

Flagged files are compared after replacing each project's *own* name with a
case-class placeholder: `core` -> lower, `Core`/`DocCreator` -> title,
`CORE` -> upper, matched case-insensitively on word boundaries (so `github`
never matches `hub`, while `core-django` normalizes as expected). Files equal
after normalization are reported in a separate `equiv` column and pass
`--strict`; byte-identical files still count as `ident`. A casing-style
mismatch (`core` vs `Veles` in the same spot) or any other difference remains
drift. Files that are not valid UTF-8 fall back to byte comparison.

## Tests

```bash
python -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/python -m pytest tests/
```
