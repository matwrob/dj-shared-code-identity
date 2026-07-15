"""Tests for the shared-code identity verifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared_code_identity import cli


# ---------------------------------------------------------------------------
# Workspace / manifest builders
# ---------------------------------------------------------------------------


def write_manifest(tmp_path: Path, body: str) -> Path:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(exist_ok=True)
    path = manifest_dir / "test.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def make_workspace(
    tmp_path: Path,
    files: dict[str, dict[str, str | bytes]],
) -> Path:
    """Build a fake workspace.

    ``files`` maps project name to a mapping of anchored relative paths
    (``src:...`` or ``root:...``) to file content.
    """
    workspace = tmp_path / "workspace"
    for project, project_files in files.items():
        for anchored, content in project_files.items():
            anchor, relative = anchored.split(":", 1)
            if anchor == "src":
                base = workspace / project / "src" / f"{project}-django"
            else:
                base = workspace / project
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
    return workspace


MANIFEST_TEMPLATE = """\
name: test
description: Test manifest.
canonical_project: core
applicable_projects:
  - core
  - veles
{body}
"""


def parse(tmp_path: Path, body: str) -> cli.Manifest:
    return cli.parse_manifest(
        write_manifest(tmp_path, MANIFEST_TEMPLATE.format(body=body))
    )


def verify(
    workspace: Path, manifest: cli.Manifest, capsys: pytest.CaptureFixture[str]
) -> tuple[tuple[int, int, int], str]:
    result = cli.verify_manifest(workspace, manifest, detail_limit=50)
    return result, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Manifest parsing and validation
# ---------------------------------------------------------------------------


class TestParseManifest:
    def test_parses_all_keys(self, tmp_path: Path) -> None:
        manifest = parse(
            tmp_path,
            "src_paths:\n"
            "  - apps/**\n"
            "src_excludes:\n"
            "  - apps/skip.py\n"
            "root_paths:\n"
            "  - dc.sh\n"
            "root_excludes: []\n"
            "project_name_insensitive:\n"
            "  - root:dc.sh\n",
        )

        assert manifest.name == "test"
        assert manifest.canonical_project == "core"
        assert manifest.applicable_projects == ("core", "veles")
        assert manifest.src_paths == ("apps/**",)
        assert manifest.src_excludes == ("apps/skip.py",)
        assert manifest.root_paths == ("dc.sh",)
        assert manifest.root_excludes == ()
        assert manifest.project_name_insensitive == ("root:dc.sh",)

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        path = write_manifest(tmp_path, "name: test\n")

        with pytest.raises(ValueError, match="missing required key"):
            cli.parse_manifest(path)

    def test_unknown_project_raises(self, tmp_path: Path) -> None:
        path = write_manifest(
            tmp_path,
            "name: test\n"
            "canonical_project: nonsuch\n"
            "applicable_projects:\n"
            "  - nonsuch\n",
        )

        with pytest.raises(ValueError, match="unknown project"):
            cli.parse_manifest(path)

    def test_canonical_not_applicable_raises(self, tmp_path: Path) -> None:
        path = write_manifest(
            tmp_path,
            "name: test\n"
            "canonical_project: core\n"
            "applicable_projects:\n"
            "  - veles\n",
        )

        with pytest.raises(ValueError, match="canonical_project must be in"):
            cli.parse_manifest(path)

    def test_name_insensitive_entry_without_anchor_raises(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="must be 'src:<pattern>' or 'root:"):
            parse(
                tmp_path,
                "root_paths:\n"
                "  - dc.sh\n"
                "project_name_insensitive:\n"
                "  - dc.sh\n",
            )


# ---------------------------------------------------------------------------
# Feature 1: include patterns must match in the canonical project
# ---------------------------------------------------------------------------


class TestUnmatchedCanonicalPatterns:
    def test_unmatched_src_pattern_raises_descriptive_error(
        self, tmp_path: Path
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"src:apps/a.py": "x\n"},
                "veles": {"src:apps/a.py": "x\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "src_paths:\n  - apps/a.py\n  - apps/missing.py\n",
        )

        with pytest.raises(ValueError) as excinfo:
            cli.verify_manifest(workspace, manifest, detail_limit=50)

        message = str(excinfo.value)
        assert "manifest 'test'" in message
        assert "'apps/missing.py'" in message
        assert "canonical project 'core'" in message
        assert str(workspace / "core" / "src" / "core-django") in message

    def test_unmatched_root_pattern_reports_repo_root(self, tmp_path: Path) -> None:
        workspace = make_workspace(tmp_path, {"core": {}, "veles": {}})
        manifest = parse(tmp_path, "root_paths:\n  - nope/**\n")

        with pytest.raises(ValueError) as excinfo:
            cli.verify_manifest(workspace, manifest, detail_limit=50)

        message = str(excinfo.value)
        assert "root: 'nope/**'" in message
        assert str(workspace / "core") in message

    def test_all_unmatched_patterns_reported_at_once(self, tmp_path: Path) -> None:
        workspace = make_workspace(tmp_path, {"core": {"root:dc.sh": "x\n"}})
        manifest = parse(
            tmp_path,
            "src_paths:\n  - apps/one.py\nroot_paths:\n  - dc.sh\n  - two.sh\n",
        )

        with pytest.raises(ValueError) as excinfo:
            cli.verify_manifest(workspace, manifest, detail_limit=50)

        message = str(excinfo.value)
        assert "2 include pattern(s)" in message
        assert "'apps/one.py'" in message
        assert "'two.sh'" in message


# ---------------------------------------------------------------------------
# Core comparison outcomes
# ---------------------------------------------------------------------------


class TestVerifyOutcomes:
    def test_identical_files_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"src:apps/a.py": "same\n"},
                "veles": {"src:apps/a.py": "same\n"},
            },
        )
        manifest = parse(tmp_path, "src_paths:\n  - apps/a.py\n")

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 0, 0)

    def test_drifted_file_counts_as_differ(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"src:apps/a.py": "one\n"},
                "veles": {"src:apps/a.py": "two\n"},
            },
        )
        manifest = parse(tmp_path, "src_paths:\n  - apps/a.py\n")

        (differ, equivalent, missing), out = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (1, 0, 0)
        assert "src:apps/a.py" in out

    def test_file_missing_in_sibling_counts_as_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"src:apps/a.py": "x\n"},
                "veles": {"src:apps/other.py": "x\n"},
            },
        )
        manifest = parse(tmp_path, "src_paths:\n  - apps/a.py\n")

        (differ, equivalent, missing), out = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 0, 1)
        assert "missing in: veles" in out

    def test_excludes_are_applied(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"src:apps/a.py": "x\n", "src:apps/skip.py": "c\n"},
                "veles": {"src:apps/a.py": "x\n", "src:apps/skip.py": "v\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "src_paths:\n  - apps/**\nsrc_excludes:\n  - apps/skip.py\n",
        )

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Feature 2: project-name-insensitive comparison
# ---------------------------------------------------------------------------


class TestProjectNameInsensitive:
    def test_name_only_difference_counts_as_equivalent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "# Start Core app\nexec core-django\n"},
                "veles": {"root:dc.sh": "# Start Veles app\nexec veles-django\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - dc.sh\nproject_name_insensitive:\n  - root:dc.sh\n",
        )

        (differ, equivalent, missing), out = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 1, 0)
        assert "Equivalent (differ only by project name):" in out
        assert "root:dc.sh" in out

    def test_same_files_not_flagged_still_differ(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "exec core-django\n"},
                "veles": {"root:dc.sh": "exec veles-django\n"},
            },
        )
        manifest = parse(tmp_path, "root_paths:\n  - dc.sh\n")

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (1, 0, 0)

    def test_camelcase_display_name_matches_title_form(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "# Core admin\n"},
                "doccreator": {"root:dc.sh": "# DocCreator admin\n"},
            },
        )
        path = write_manifest(
            tmp_path,
            "name: test\n"
            "description: t\n"
            "canonical_project: core\n"
            "applicable_projects:\n"
            "  - core\n"
            "  - doccreator\n"
            "root_paths:\n"
            "  - dc.sh\n"
            "project_name_insensitive:\n"
            "  - root:dc.sh\n",
        )
        manifest = cli.parse_manifest(path)

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 1, 0)

    def test_case_style_mismatch_is_drift(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "# core admin\n"},
                "veles": {"root:dc.sh": "# Veles admin\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - dc.sh\nproject_name_insensitive:\n  - root:dc.sh\n",
        )

        (differ, equivalent, missing), out = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (1, 0, 0)
        assert "(differs beyond project name)" in out

    def test_word_boundary_protects_embedded_names(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # "github" contains "hub" but must not be normalized in hub's file.
        workspace = make_workspace(
            tmp_path,
            {
                "hub": {"root:dc.sh": "repo: https://github.com/x\nname: hub\n"},
                "mailman": {
                    "root:dc.sh": "repo: https://github.com/x\nname: mailman\n"
                },
            },
        )
        path = write_manifest(
            tmp_path,
            "name: test\n"
            "description: t\n"
            "canonical_project: hub\n"
            "applicable_projects:\n"
            "  - hub\n"
            "  - mailman\n"
            "root_paths:\n"
            "  - dc.sh\n"
            "project_name_insensitive:\n"
            "  - root:dc.sh\n",
        )
        manifest = cli.parse_manifest(path)

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 1, 0)

    def test_real_drift_beyond_name_still_reported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "exec core-django --workers 9\n"},
                "veles": {"root:dc.sh": "exec veles-django --workers 4\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - dc.sh\nproject_name_insensitive:\n  - root:dc.sh\n",
        )

        (differ, equivalent, missing), out = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (1, 0, 0)
        assert "(differs beyond project name)" in out

    def test_byte_identical_flagged_file_counts_as_identical(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "no names here\n"},
                "veles": {"root:dc.sh": "no names here\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - dc.sh\nproject_name_insensitive:\n  - root:dc.sh\n",
        )

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 0, 0)

    def test_flag_pattern_matching_no_tracked_file_raises(
        self, tmp_path: Path
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "x\n"},
                "veles": {"root:dc.sh": "x\n"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n"
            "  - dc.sh\n"
            "project_name_insensitive:\n"
            "  - root:.pre-commit-config.yaml\n",
        )

        with pytest.raises(ValueError, match="matched no tracked files"):
            cli.verify_manifest(workspace, manifest, detail_limit=50)

    def test_binary_flagged_file_falls_back_to_byte_comparison(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {"root:logo.png": b"\x89PNG\xff\xfecore"},
                "veles": {"root:logo.png": b"\x89PNG\xff\xfeveles"},
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - logo.png\nproject_name_insensitive:\n  - root:logo.png\n",
        )

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (1, 0, 0)

    def test_glob_flag_pattern_covers_multiple_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workspace = make_workspace(
            tmp_path,
            {
                "core": {
                    "root:dc.sh": "# Core\n",
                    "root:dc.bat": "REM Core\n",
                },
                "veles": {
                    "root:dc.sh": "# Veles\n",
                    "root:dc.bat": "REM Veles\n",
                },
            },
        )
        manifest = parse(
            tmp_path,
            "root_paths:\n  - dc.sh\n  - dc.bat\nproject_name_insensitive:\n  - root:dc.*\n",
        )

        (differ, equivalent, missing), _ = verify(workspace, manifest, capsys)

        assert (differ, equivalent, missing) == (0, 2, 0)


class TestNormalizedDigest:
    @pytest.mark.parametrize(
        ("token", "placeholder"),
        [
            ("core", cli._PLACEHOLDER_LOWER),
            ("Core", cli._PLACEHOLDER_TITLE),
            ("CORE", cli._PLACEHOLDER_UPPER),
            ("cOrE", cli._PLACEHOLDER_TITLE),
        ],
    )
    def test_case_class_placeholders(self, token: str, placeholder: str) -> None:
        assert cli._placeholder(token) == placeholder

    def test_uppercase_project_names_align(self, tmp_path: Path) -> None:
        core = tmp_path / "core.txt"
        veles = tmp_path / "veles.txt"
        core.write_text("ENV=CORE prefix\n", encoding="utf-8")
        veles.write_text("ENV=VELES prefix\n", encoding="utf-8")

        assert cli.normalized_digest(core, "core") == cli.normalized_digest(
            veles, "veles"
        )

    def test_underscore_compounds_are_normalized(self, tmp_path: Path) -> None:
        # Underscores count as separators: snake_case log names and env-var
        # tokens embedding the project name must align across projects.
        core = tmp_path / "core.txt"
        veles = tmp_path / "veles.txt"
        core.write_text(
            "log core_media_access.log tag ${CORE_IMAGE_TAG}\n", encoding="utf-8"
        )
        veles.write_text(
            "log veles_media_access.log tag ${VELES_IMAGE_TAG}\n", encoding="utf-8"
        )

        assert cli.normalized_digest(core, "core") == cli.normalized_digest(
            veles, "veles"
        )

    def test_only_own_project_name_is_normalized(self, tmp_path: Path) -> None:
        # A veles file mentioning "core" keeps that token verbatim, so it
        # cannot accidentally align with a core file that had "core" replaced.
        core = tmp_path / "core.txt"
        veles = tmp_path / "veles.txt"
        core.write_text("uses core backup\n", encoding="utf-8")
        veles.write_text("uses core backup\n", encoding="utf-8")

        assert cli.normalized_digest(core, "core") != cli.normalized_digest(
            veles, "veles"
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


class TestMain:
    def run_main(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *extra_args: str,
    ) -> int:
        workspace = tmp_path / "workspace"
        manifest_dir = tmp_path / "manifests"
        argv = [
            "cli",
            "--workspace",
            str(workspace),
            "--manifest-dir",
            str(manifest_dir),
            *extra_args,
        ]
        monkeypatch.setattr(cli.sys, "argv", argv)
        return cli.main()

    def test_strict_fails_on_drift_but_not_on_equivalent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "# Core\n"},
                "veles": {"root:dc.sh": "# Veles\n"},
            },
        )
        write_manifest(
            tmp_path,
            MANIFEST_TEMPLATE.format(
                body="root_paths:\n  - dc.sh\nproject_name_insensitive:\n  - root:dc.sh\n"
            ),
        )

        exit_code = self.run_main(tmp_path, monkeypatch, "--strict")

        assert exit_code == 0
        assert "equiv=1" in capsys.readouterr().out

    def test_strict_fails_on_real_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_workspace(
            tmp_path,
            {
                "core": {"root:dc.sh": "one\n"},
                "veles": {"root:dc.sh": "two\n"},
            },
        )
        write_manifest(
            tmp_path, MANIFEST_TEMPLATE.format(body="root_paths:\n  - dc.sh\n")
        )

        exit_code = self.run_main(tmp_path, monkeypatch, "--strict")

        assert exit_code == 1
        assert "FAIL" in capsys.readouterr().err

    def test_unmatched_pattern_exits_2_without_strict(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_workspace(tmp_path, {"core": {"root:dc.sh": "x\n"}})
        write_manifest(
            tmp_path, MANIFEST_TEMPLATE.format(body="root_paths:\n  - nope.sh\n")
        )

        exit_code = self.run_main(tmp_path, monkeypatch)

        assert exit_code == 2
        assert "matched no files in canonical project 'core'" in capsys.readouterr().err


class TestSync:
    """--sync copies canonical bytes over drifted/missing siblings."""

    def make(self, tmp_path):
        workspace = make_workspace(
            tmp_path,
            {
                "core": {
                    "src:requirements/shared/base.in": "Django==5.2.16\n",
                    "root:dc.sh": "# Core\n",
                },
                "veles": {
                    "src:requirements/shared/base.in": "Django==5.2.15\n",
                    "root:dc.sh": "# Veles\n",
                },
            },
        )
        write_manifest(
            tmp_path,
            MANIFEST_TEMPLATE.format(
                body=(
                    "src_paths:\n"
                    "  - requirements/shared/base.in\n"
                    "root_paths:\n"
                    "  - dc.sh\n"
                    "project_name_insensitive:\n"
                    "  - root:dc.sh\n"
                )
            ),
        )
        return workspace

    def run_cli(self, tmp_path, monkeypatch, *extra):
        argv = [
            "cli",
            "--workspace",
            str(tmp_path / "workspace"),
            "--manifest-dir",
            str(tmp_path / "manifests"),
            *extra,
        ]
        monkeypatch.setattr(cli.sys, "argv", argv)
        return cli.main()

    def test_dry_run_plans_but_writes_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        workspace = self.make(tmp_path)

        exit_code = self.run_cli(tmp_path, monkeypatch, "--sync")

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "would sync src:requirements/shared/base.in -> veles" in out
        assert "SKIP (name-insensitive) root:dc.sh -> veles" in out
        target = (
            workspace / "veles" / "src" / "veles-django"
            / "requirements" / "shared" / "base.in"
        )
        assert target.read_text() == "Django==5.2.15\n"

    def test_apply_writes_canonical_bytes(self, tmp_path, monkeypatch, capsys):
        workspace = self.make(tmp_path)

        exit_code = self.run_cli(tmp_path, monkeypatch, "--sync", "--apply")

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "copies written=1" in out
        target = (
            workspace / "veles" / "src" / "veles-django"
            / "requirements" / "shared" / "base.in"
        )
        assert target.read_text() == "Django==5.2.16\n"
        # name-insensitive file untouched
        assert (workspace / "veles" / "dc.sh").read_text() == "# Veles\n"

    def test_apply_creates_missing_files(self, tmp_path, monkeypatch):
        workspace = self.make(tmp_path)
        (
            workspace / "veles" / "src" / "veles-django"
            / "requirements" / "shared" / "base.in"
        ).unlink()

        self.run_cli(tmp_path, monkeypatch, "--sync", "--apply")

        target = (
            workspace / "veles" / "src" / "veles-django"
            / "requirements" / "shared" / "base.in"
        )
        assert target.read_text() == "Django==5.2.16\n"

    def test_apply_without_sync_is_an_error(
        self, tmp_path, monkeypatch, capsys
    ):
        self.make(tmp_path)

        exit_code = self.run_cli(tmp_path, monkeypatch, "--apply")

        assert exit_code == 2
        assert "--apply requires --sync" in capsys.readouterr().err
