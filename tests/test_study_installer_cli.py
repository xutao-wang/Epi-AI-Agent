from __future__ import annotations

from study_installer import configure_study_root, main
from study_package.registry import load_registry
from tests.study_package_fixtures import (
    create_package_archive,
    create_package_archive_from_root,
    create_package_root,
    minimal_manifest,
)


def test_cli_installs_local_study_archive_into_explicit_study_root(tmp_path, capsys) -> None:
    archive = create_package_archive(tmp_path / "source")
    study_root = tmp_path / "study_data"

    exit_code = main(
        ["--study", str(archive), "--study-root", str(study_root)]
    )

    assert exit_code == 0
    assert load_registry(study_root / "studies").active == {
        "example-study": "1.0.0"
    }
    assert "Installed: example-study@1.0.0" in capsys.readouterr().out


def test_cli_expected_identity_mismatch_does_not_mutate_installed_studies(
    tmp_path,
    capsys,
) -> None:
    study_root = tmp_path / "study_data"
    baseline_archive = create_package_archive(
        tmp_path / "baseline",
        manifest=minimal_manifest(study_id="baseline-study", package_version="1.0.0"),
    )
    assert main(
        ["--study", str(baseline_archive), "--study-root", str(study_root)]
    ) == 0
    unexpected_archive = create_package_archive(
        tmp_path / "unexpected",
        manifest=minimal_manifest(
            study_id="unexpected-study",
            package_version="9.9.9",
        ),
    )

    exit_code = main(
        [
            "--study",
            str(unexpected_archive),
            "--study-root",
            str(study_root),
            "--expected-study-id",
            "requested-study",
            "--expected-package-version",
            "2.0.0",
        ]
    )

    assert exit_code == 1
    assert "does not match expected identity requested-study@2.0.0" in (
        capsys.readouterr().err
    )
    studies_root = study_root / "studies"
    assert load_registry(studies_root).active == {"baseline-study": "1.0.0"}
    assert not (studies_root / "packages" / "unexpected-study").exists()


def test_cli_uses_saved_study_root_setting(
    tmp_path,
    capsys,
    monkeypatch,
) -> None:
    archive = create_package_archive(tmp_path / "source")
    configured_study_root = tmp_path / "configured-study-data"
    monkeypatch.setenv("REPORT_AGENT_STUDY_ROOT", str(configured_study_root))
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--study", str(archive)])

    assert exit_code == 0
    assert load_registry(configured_study_root / "studies").active == {
        "example-study": "1.0.0"
    }
    assert not (tmp_path / "study_data").exists()
    assert "Installed: example-study@1.0.0" in capsys.readouterr().out


def test_configure_study_root_uses_default_and_saves_selection(tmp_path) -> None:
    environ: dict[str, str] = {}

    selected = configure_study_root(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: "",
        persist=False,
    )

    assert selected == (tmp_path / "study_data").resolve()
    assert selected.is_dir()
    assert environ["REPORT_AGENT_STUDY_ROOT"] == str(selected)


def test_cli_rejects_invalid_activation_target(tmp_path, capsys) -> None:
    exit_code = main(
        ["--activate", "not-a-target", "--study-root", str(tmp_path / "study_data")]
    )

    assert exit_code == 1
    assert "ERROR:" in capsys.readouterr().err


def test_cli_rejects_expected_identity_guard_for_activation(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "--activate",
            "example-study@1.0.0",
            "--study-root",
            str(tmp_path / "study_data"),
            "--expected-study-id",
            "example-study",
            "--expected-package-version",
            "1.0.0",
        ]
    )

    assert exit_code == 1
    assert "expected identity options are only valid with --study" in (
        capsys.readouterr().err
    )


def test_cli_prints_nonfatal_unconsumed_file_warning(tmp_path, capsys) -> None:
    package_root = create_package_root(
        tmp_path / "source",
        manifest=minimal_manifest(
            format_version=3,
            study_design_format="markdown",
        ),
    )
    asset = package_root / "study-design" / "diagram.png"
    asset.write_bytes(b"asset")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    exit_code = main(
        ["--study", str(archive), "--study-root", str(tmp_path / "study_data")]
    )

    output = capsys.readouterr()
    assert exit_code == 0
    assert "Warning: diagram.png is not consumed by study-design indexing" in output.out
    assert "Installed: example-study@1.0.0" in output.out
    assert "ERROR" not in output.out
