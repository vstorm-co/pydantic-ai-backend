"""A live session's files, handled on the host without following a single link.

The directory these functions work in is written by untrusted code in a
container, which can plant a symlink anywhere at any moment. Each test that plants
one checks the same thing: the host file it points at is neither read, written
nor removed, and the call says the sandbox has to resolve it instead.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from pydantic_ai_backends.remote import _hostfs as hostfs
from pydantic_ai_backends.remote._hostfs import NotNative

WORK = "/workspace"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "s-1" / "workspace"
    workspace.mkdir(parents=True)
    return workspace


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A host file outside the workspace, the target of every planted link."""
    target = tmp_path / "host-secret"
    target.write_text("the host's own file")
    return target


def _parts(path: str) -> list[str]:
    return hostfs.parts_of(path, WORK)


class TestWhichPathsAreNative:
    def test_the_components_below_the_work_directory(self) -> None:
        assert _parts("/workspace/src/main.py") == ["src", "main.py"]
        assert _parts("/workspace") == []

    def test_a_path_outside_the_work_directory_is_the_sandboxs(self) -> None:
        with pytest.raises(NotNative, match="outside"):
            _parts("/etc/passwd")

    def test_a_path_that_climbs_is_the_sandboxs(self) -> None:
        with pytest.raises(NotNative, match=r"\.\."):
            _parts("/workspace/a/../../etc/passwd")

    def test_a_relative_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            _parts("notes.md")


class TestReading:
    def test_a_file_reads_back(self, root: Path) -> None:
        (root / "notes.md").write_bytes(b"hello")
        assert hostfs.read(root, ["notes.md"], max_bytes=100) == b"hello"

    def test_a_missing_file_is_file_not_found(self, root: Path) -> None:
        with pytest.raises(FileNotFoundError):
            hostfs.read(root, ["absent"], max_bytes=100)

    def test_a_directory_is_not_read(self, root: Path) -> None:
        (root / "dir").mkdir()
        with pytest.raises(IsADirectoryError):
            hostfs.read(root, ["dir"], max_bytes=100)
        with pytest.raises(IsADirectoryError):
            hostfs.read(root, [], max_bytes=100)

    def test_a_file_over_the_limit_goes_through_the_sandbox(self, root: Path) -> None:
        (root / "big").write_bytes(b"x" * 101)
        with pytest.raises(NotNative, match="limit"):
            hostfs.read(root, ["big"], max_bytes=100)

    def test_a_link_to_a_host_file_is_not_followed(self, root: Path, secret: Path) -> None:
        (root / "notes.md").symlink_to(secret)
        with pytest.raises(NotNative, match="symbolic link"):
            hostfs.read(root, ["notes.md"], max_bytes=100)

    def test_a_link_on_the_way_is_not_followed(self, root: Path, secret: Path) -> None:
        (root / "dir").symlink_to(secret.parent)
        with pytest.raises(NotNative, match="symbolic link"):
            hostfs.read(root, ["dir", secret.name], max_bytes=100)

    def test_a_fifo_is_neither_read_nor_waited_on(self, root: Path) -> None:
        os.mkfifo(root / "pipe")
        with pytest.raises(NotNative, match="regular"):
            hostfs.read(root, ["pipe"], max_bytes=100)

    def test_a_file_under_a_file_is_not_a_directory(self, root: Path) -> None:
        (root / "file").write_text("x")
        with pytest.raises(NotADirectoryError):
            hostfs.read(root, ["file", "child"], max_bytes=100)


class TestWriting:
    def test_parents_are_made_and_the_bytes_land(self, root: Path) -> None:
        hostfs.write(root, ["a", "b", "c.txt"], b"data")
        assert (root / "a" / "b" / "c.txt").read_bytes() == b"data"

    def test_an_existing_file_keeps_its_mode(self, root: Path) -> None:
        script = root / "run.sh"
        script.write_text("old")
        script.chmod(0o755)

        hostfs.write(root, ["run.sh"], b"#!/bin/sh\n")

        assert stat.S_IMODE(script.stat().st_mode) == 0o755
        assert script.read_bytes() == b"#!/bin/sh\n"

    def test_a_link_is_not_written_through_on_the_host(self, root: Path, secret: Path) -> None:
        (root / "notes.md").symlink_to(secret)
        with pytest.raises(NotNative):
            hostfs.write(root, ["notes.md"], b"overwritten")
        assert secret.read_text() == "the host's own file"

    def test_a_linked_parent_is_not_written_into(self, root: Path, secret: Path) -> None:
        (root / "dir").symlink_to(secret.parent)
        with pytest.raises(NotNative):
            hostfs.write(root, ["dir", "planted"], b"x")
        assert not (secret.parent / "planted").exists()

    def test_a_directory_is_not_overwritten(self, root: Path) -> None:
        (root / "dir").mkdir()
        with pytest.raises(IsADirectoryError):
            hostfs.write(root, ["dir"], b"x")
        with pytest.raises(IsADirectoryError):
            hostfs.write(root, [], b"x")

    def test_a_fifo_with_no_reader_is_the_sandboxs(self, root: Path) -> None:
        os.mkfifo(root / "pipe")
        with pytest.raises(NotNative, match="regular"):
            hostfs.write(root, ["pipe"], b"x")

    def test_a_file_where_a_parent_goes_is_not_a_directory(self, root: Path) -> None:
        (root / "file").write_text("x")
        with pytest.raises(NotADirectoryError):
            hostfs.write(root, ["file", "child"], b"x")

    def test_a_special_file_opened_for_writing_is_refused(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FIFO that has a reader opens; it is still not a file to write."""
        (root / "pipe").write_text("x")
        real_fstat = os.fstat

        def as_fifo(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            fields = list(info)
            fields[0] = stat.S_IFIFO | 0o644
            return os.stat_result(fields)

        monkeypatch.setattr(hostfs.os, "fstat", as_fifo)
        with pytest.raises(NotNative, match="regular"):
            hostfs.write(root, ["pipe"], b"x")


class TestDescribing:
    def test_a_file_and_a_directory(self, root: Path) -> None:
        (root / "f.txt").write_bytes(b"12345")
        (root / "d").mkdir()

        assert hostfs.stat(root, ["f.txt"], "/workspace/f.txt") == hostfs.Entry(
            name="f.txt", path="/workspace/f.txt", is_dir=False, size=5
        )
        assert hostfs.stat(root, ["d"], "/workspace/d").is_dir
        assert hostfs.stat(root, [], "/workspace").is_dir

    def test_a_link_is_described_by_the_sandbox(self, root: Path, secret: Path) -> None:
        (root / "l").symlink_to(secret)
        with pytest.raises(NotNative):
            hostfs.stat(root, ["l"], "/workspace/l")

    def test_a_listing_is_sorted_and_carries_full_paths(self, root: Path) -> None:
        (root / "b.txt").write_bytes(b"xy")
        (root / "a").mkdir()

        entries = hostfs.list_dir(root, [], "/workspace")

        assert [(e.name, e.path, e.is_dir, e.size) for e in entries] == [
            ("a", "/workspace/a", True, None),
            ("b.txt", "/workspace/b.txt", False, 2),
        ]

    def test_a_directory_holding_a_link_is_listed_by_the_sandbox(
        self, root: Path, secret: Path
    ) -> None:
        (root / "l").symlink_to(secret)
        with pytest.raises(NotNative):
            hostfs.list_dir(root, [], "/workspace")


class TestMakingDirectories:
    def test_every_missing_parent_is_made(self, root: Path) -> None:
        hostfs.make_dir(root, ["a", "b", "c"])
        assert (root / "a" / "b" / "c").is_dir()

    def test_an_existing_directory_is_fine(self, root: Path) -> None:
        (root / "a").mkdir()
        hostfs.make_dir(root, ["a"])
        hostfs.make_dir(root, [])

    def test_a_file_where_it_goes_already_exists(self, root: Path) -> None:
        (root / "file").write_text("x")
        with pytest.raises(FileExistsError):
            hostfs.make_dir(root, ["file"])

    def test_a_link_where_it_goes_is_the_sandboxs(self, root: Path, secret: Path) -> None:
        (root / "l").symlink_to(secret.parent)
        with pytest.raises(NotNative):
            hostfs.make_dir(root, ["l"])

    def test_a_link_on_the_way_is_not_followed(self, root: Path, secret: Path) -> None:
        (root / "l").symlink_to(secret.parent)
        with pytest.raises(NotNative):
            hostfs.make_dir(root, ["l", "planted"])
        assert not (secret.parent / "planted").exists()

    def test_a_parent_that_cannot_be_opened_raises_as_is(self, root: Path) -> None:
        (root / "file").write_text("x")
        with pytest.raises(NotADirectoryError):
            hostfs.make_dir(root, ["file", "child", "grandchild"])


class TestRemoving:
    def test_a_file(self, root: Path) -> None:
        (root / "f").write_text("x")
        hostfs.remove(root, ["f"])
        assert not (root / "f").exists()

    def test_a_tree_and_the_links_in_it_but_not_their_targets(
        self, root: Path, secret: Path
    ) -> None:
        tree = root / "tree" / "deep"
        tree.mkdir(parents=True)
        (tree / "f").write_text("x")
        (root / "tree" / "link").symlink_to(secret)
        (root / "tree" / "dirlink").symlink_to(secret.parent)

        hostfs.remove(root, ["tree"])

        assert not (root / "tree").exists()
        assert secret.read_text() == "the host's own file"

    def test_a_link_itself_and_not_its_target(self, root: Path, secret: Path) -> None:
        (root / "l").symlink_to(secret)
        hostfs.remove(root, ["l"])
        assert not (root / "l").is_symlink()
        assert secret.exists()

    def test_the_work_directory_is_refused(self, root: Path) -> None:
        with pytest.raises(ValueError, match="working directory"):
            hostfs.remove(root, [])

    def test_a_missing_path_is_file_not_found(self, root: Path) -> None:
        with pytest.raises(FileNotFoundError):
            hostfs.remove(root, ["absent"])

    def test_a_directory_swapped_for_a_link_midway_is_left_to_the_sandbox(
        self, root: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race the descriptors exist for: listed as a directory, a link by the
        time it is opened."""
        (root / "tree" / "sub").mkdir(parents=True)
        real_open = os.open

        def swapped(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            if path == "sub":
                os.rmdir("sub", dir_fd=dir_fd)
                os.symlink(str(secret.parent), "sub", dir_fd=dir_fd)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(hostfs.os, "open", swapped)
        with pytest.raises(NotNative):
            hostfs.remove(root, ["tree"])
        assert secret.exists()


def test_a_link_seen_only_as_not_a_directory_is_still_a_link(
    root: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some systems answer `ENOTDIR` rather than `ELOOP` for a link opened as a
    directory; it is told apart by looking at what is there."""
    (root / "l").symlink_to(secret)
    real_open = os.open

    def not_a_directory(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == "l":
            raise NotADirectoryError(20, "Not a directory")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(hostfs.os, "open", not_a_directory)
    with pytest.raises(NotNative):
        hostfs.read(root, ["l", "child"], max_bytes=100)


def test_a_vanished_entry_reads_as_no_link(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert hostfs._is_link("absent", fd) is False
    finally:
        os.close(fd)


def test_a_missing_directory_on_the_way_is_file_not_found(root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        hostfs.read(root, ["absent", "file"], max_bytes=100)


def test_a_parent_made_and_swapped_for_a_link_is_not_entered(
    root: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between making a missing parent and opening it."""
    real_mkdir = os.mkdir

    def mkdir_then_swap(path: str, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        os.rmdir(path, dir_fd=dir_fd)
        os.symlink(str(secret.parent), path, dir_fd=dir_fd)

    monkeypatch.setattr(hostfs.os, "mkdir", mkdir_then_swap)
    with pytest.raises(NotNative):
        hostfs.write(root, ["new", "file"], b"x")
    assert not (secret.parent / "file").exists()
