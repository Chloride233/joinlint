from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject, resolve_project_root
from tests.conftest import make_project


def test_nearest_joinlint_project_is_selected(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    nested = project / "nested" / "deeper"
    nested.mkdir(parents=True)

    assert resolve_project_root(nested) == project


def test_intermediate_symlink_component_is_rejected(project: Path) -> None:
    (project / "outside").mkdir()
    (project / "link").symlink_to(project / "outside", target_is_directory=True)

    with SafeProject(project) as safe_project:
        with pytest.raises(JoinLintError) as captured:
            safe_project.open_relative(PurePosixPath("link/data.csv"), os.O_RDONLY)
    assert captured.value.code == "SYMLINK_NOT_ALLOWED"


def test_final_symlink_component_is_rejected(project: Path) -> None:
    (project / "source.csv").write_text("id\n1\n", encoding="utf-8")
    (project / "link.csv").symlink_to(project / "source.csv")

    with SafeProject(project) as safe_project:
        with pytest.raises(JoinLintError) as captured:
            safe_project.open_relative(PurePosixPath("link.csv"), os.O_RDONLY)
    assert captured.value.code == "SYMLINK_NOT_ALLOWED"


def test_parent_component_is_rejected(project: Path) -> None:
    with SafeProject(project) as safe_project:
        with pytest.raises(JoinLintError) as captured:
            safe_project.open_relative(PurePosixPath("../outside.csv"), os.O_RDONLY)
    assert captured.value.code == "PATH_OUTSIDE_PROJECT"


def test_open_relative_reads_a_regular_file(project: Path) -> None:
    (project / "data.csv").write_text("id\n1\n", encoding="utf-8")

    with SafeProject(project) as safe_project:
        descriptor = safe_project.open_relative(PurePosixPath("data.csv"), os.O_RDONLY)
        try:
            assert os.read(descriptor, 32) == b"id\n1\n"
        finally:
            os.close(descriptor)


def test_open_relative_remains_anchored_after_root_path_is_replaced(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    (project / "data.csv").write_text("id\nold\n", encoding="utf-8")

    with SafeProject(project) as safe_project:
        project.rename(tmp_path / "replaced")
        project.mkdir()
        (project / "data.csv").write_text("id\nnew\n", encoding="utf-8")

        descriptor = safe_project.open_relative(PurePosixPath("data.csv"), os.O_RDONLY)
        try:
            assert os.read(descriptor, 32) == b"id\nold\n"
        finally:
            os.close(descriptor)


def test_non_regular_final_path_is_rejected(project: Path) -> None:
    os.mkfifo(project / "pipe")

    with SafeProject(project) as safe_project:
        with pytest.raises(JoinLintError) as captured:
            safe_project.open_relative(PurePosixPath("pipe"), os.O_RDONLY | os.O_NONBLOCK)
    assert captured.value.code == "UNSUPPORTED_SOURCE"
