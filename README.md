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
```

`src_paths` are resolved relative to `<project>/src/<project>-django/`.
`root_paths` are resolved relative to the project repository root. Exclusions
are applied after inclusions and support the same glob syntax.

Overlapping manifests are valid. A file can be governed by `base.yaml` and by a
more specific manifest; each manifest reports its own byte-identity result.
