"""Tests for audit writable roots for overlapping Git checkouts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_host_example import (
    bounded_file_reads,
    unusual_git_metadata,
)

from tbhbdev.alpha.agent import (
    worktree_writes,
)


def test_sandbox_placeholders_inside_git_are_no_main_checkout(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    (mount / ".git" / "hooks").mkdir(parents=True)
    (mount / ".git" / "config").write_text("")
    assert not worktree_writes.is_main_checkout(mount)
    (mount / ".git" / "HEAD").write_text("")
    assert not worktree_writes.is_main_checkout(mount)
    (mount / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert worktree_writes.is_main_checkout(mount)


def test_sandbox_mount_points_above_a_write_root_are_allowed(
    checkout_paths: tuple[Path, Path],
) -> None:
    source, workspace = checkout_paths
    shared = source.parent / "scratch" / "tools"
    (shared / ".git").mkdir(parents=True)
    (shared / "run").mkdir()
    worktree_writes.validate_writes(workspace, source, (str(shared / "run"),))
    (shared / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    found = worktree_writes.validate_writes(workspace, source, (str(shared / "run"),))
    assert any("reaches main checkout" in line for line in found)


@pytest.fixture
def checkout_paths(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git/HEAD").write_text("ref: refs/heads/main\n")
    workspace = source / ".worktrees" / "agent-example"
    (workspace / ".git").mkdir(parents=True)
    return source, workspace


@pytest.mark.parametrize(
    "suffix", ["", ".git", ".git/hooks", "src", ".worktrees/other"]
)
@pytest.mark.parametrize("alias", [False, True])
def test_write_roots_refuse_main_checkout_paths(
    tmp_path: Path, checkout_paths: tuple[Path, Path], suffix: str, alias: bool
) -> None:
    source, workspace = checkout_paths
    root = source / suffix
    if alias:
        link = tmp_path / "tmp-alias"
        link.symlink_to(source, target_is_directory=True)
        root = link / suffix
    found = worktree_writes.validate_writes(workspace, source, (str(root),))
    assert any("main checkout" in line for line in found)
    assert found[0] == f"write root {root.resolve()} reaches main checkout {source}"


@pytest.mark.parametrize("suffix", ["", ".git", ".git/hooks", "src"])
def test_workspace_cannot_be_a_main_checkout(
    checkout_paths: tuple[Path, Path], suffix: str
) -> None:
    source, _ = checkout_paths
    workspace = source / suffix
    found = worktree_writes.validate_writes(workspace, source, ())
    assert any("main checkout" in line for line in found)


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_write_roots_scan_three_levels_for_other_clones(
    tmp_path: Path, checkout_paths: tuple[Path, Path], depth: int
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    clone = root.joinpath(*[f"level-{index}" for index in range(depth)])
    (clone / ".git").mkdir(parents=True)
    (clone / ".git/HEAD").write_text("ref: refs/heads/main\n")
    found = worktree_writes.validate_writes(workspace, source, (str(root),))
    assert any("main checkout" in line for line in found)
    relation = "reaches" if depth == 0 else "contains"
    assert found[0] == f"write root {root} {relation} main checkout {clone}"


def test_source_ancestors_are_refused_without_scanning(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    found = worktree_writes.validate_writes(workspace, source, (str(tmp_path),))
    assert any("main checkout" in line for line in found)
    assert found[0] == f"write root {tmp_path} contains main checkout {source}"


def test_only_this_workspace_is_allowed_under_main_checkout(
    checkout_paths: tuple[Path, Path],
) -> None:
    source, workspace = checkout_paths
    worktree_writes.validate_writes(workspace, source, (str(workspace / "src"),))
    other = workspace.parent / "agent-other"
    (other / ".git").mkdir(parents=True)
    found = worktree_writes.validate_writes(workspace, source, (str(other),))
    assert any("main checkout" in line for line in found)


def test_scan_is_bounded_and_does_not_follow_links(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    (root / "a/b/c/d/.git").mkdir(parents=True)
    (root / "neighbor").symlink_to(source, target_is_directory=True)
    (root / "ordinary-file").touch()
    linked_git = root / "linked-git"
    linked_git.mkdir()
    (linked_git / ".git").symlink_to(source / ".git", target_is_directory=True)
    worktree_writes.validate_writes(
        workspace, source, (str(root), str(tmp_path / "missing"))
    )


def test_write_root_neighboring_a_clone_is_allowed(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = source.with_name(source.name + "-cache")
    root.mkdir()
    worktree_writes.validate_writes(workspace, source, (str(root),))
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n")
    worktree_writes.validate_writes(workspace, source, (str(linked),))


@pytest.mark.parametrize("target", [".", "../elsewhere"])
def test_main_checkout_requires_its_own_common_directory(
    tmp_path: Path, checkout_paths: tuple[Path, Path], target: str
) -> None:
    source, workspace = checkout_paths
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git/commondir").write_text(target + "\n")
    (other / ".git/HEAD").write_text("ref: refs/heads/main\n")
    if target == ".":
        found = worktree_writes.validate_writes(workspace, source, (str(other),))
        assert any("main checkout" in line for line in found)
    else:
        worktree_writes.validate_writes(workspace, source, (str(other),))


def test_symlinked_tmp_alias_cannot_hide_a_clone(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    temporary = tmp_path / "private/tmp"
    temporary.mkdir(parents=True)
    (temporary / "clone/.git").mkdir(parents=True)
    (temporary / "clone/.git/HEAD").write_text("ref: refs/heads/main\n")
    alias = tmp_path / "tmp"
    alias.symlink_to(temporary, target_is_directory=True)
    found = worktree_writes.validate_writes(workspace, source, (str(alias),))
    assert any("main checkout" in line for line in found)
    assert any(str(temporary) in line for line in found)


def test_checkout_detection_reads_head_of_a_real_git_directory(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / ".git").mkdir(parents=True)
    (real / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    link = tmp_path / "link"
    link.mkdir()
    (link / ".git").symlink_to(real / ".git")
    assert worktree_writes.is_main_checkout(real)
    assert not worktree_writes.is_main_checkout(link)
    head_dir = tmp_path / "head-dir"
    (head_dir / ".git" / "HEAD").mkdir(parents=True)
    assert not worktree_writes.is_main_checkout(head_dir)


def test_empty_sandbox_mount_points_are_cleared_before_the_overlap_check(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    for name in worktree_writes.SANDBOX_MOUNT_POINTS:
        (root / name).mkdir(parents=True)
    worktree_writes.validate_writes(workspace, source, (str(root),))
    assert not any(
        (root / name).exists() for name in worktree_writes.SANDBOX_MOUNT_POINTS
    )


def test_mount_point_holding_a_file_stays_and_still_refuses(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    (root / ".git").mkdir(parents=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (root / ".codex").mkdir()
    (root / ".codex/auth").touch()
    found = worktree_writes.validate_writes(workspace, source, (str(root),))
    assert any("main checkout" in line for line in found)
    assert (root / ".codex/auth").exists()


def test_symlinked_mount_point_is_left_alone(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = tmp_path / "writable"
    root.mkdir()
    (root / ".aws").symlink_to(target, target_is_directory=True)
    worktree_writes.validate_writes(workspace, source, (str(root),))
    assert (root / ".aws").is_symlink()
    assert target.is_dir()


def test_mount_point_that_cannot_be_removed_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".agents").mkdir()

    def refuse(self: Path) -> None:
        raise PermissionError(self)

    monkeypatch.setattr(Path, "rmdir", refuse)
    worktree_writes.clear_mount_points((str(tmp_path),))
    assert (tmp_path / ".agents").is_dir()


@pytest.mark.parametrize(
    ("entry", "kind"),
    [("commondir", "directory"), ("commondir", "fifo"), ("HEAD", "fifo")],
)
def test_unusual_git_metadata_is_only_an_audit_finding(
    tmp_path: Path, checkout_paths: tuple[Path, Path], entry: str, kind: str
) -> None:
    source, workspace = checkout_paths
    root = unusual_git_metadata(tmp_path, entry, kind)
    findings: list[str] = []
    assert not worktree_writes.is_main_checkout(root, findings)
    assert any(f"{entry} is not a regular file" in line for line in findings)
    recorded = worktree_writes.validate_writes(workspace, source, (str(root),))
    assert any("cannot audit main checkout" in line for line in recorded)
    assert any("writable\nforged" in line for line in recorded)


@pytest.mark.parametrize(
    "failure", [PermissionError("unreadable"), UnicodeError("invalid text")]
)
def test_checkout_audit_records_metadata_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")

    def refuse(_directory: Path) -> bool:
        raise failure

    monkeypatch.setattr(worktree_writes, "_own_common_directory", refuse)
    findings: list[str] = []
    assert not worktree_writes.is_main_checkout(tmp_path, findings)
    assert findings == [f"cannot audit main checkout {tmp_path}: {failure}"]
    assert not worktree_writes.is_main_checkout(tmp_path)


def test_common_directory_replaced_with_fifo_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git/commondir").write_text(".\n")
    regular_fstat = os.fstat

    def fifo_stat(fd: int) -> os.stat_result:
        metadata = regular_fstat(fd)
        return os.stat_result((0o010600, *metadata[1:]))

    monkeypatch.setattr(os, "fstat", fifo_stat)
    findings: list[str] = []
    assert not worktree_writes.is_main_checkout(tmp_path, findings)
    assert findings == [
        (
            f"cannot audit main checkout {tmp_path}: "
            f"{tmp_path / '.git/commondir'} is not a regular file"
        )
    ]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b".\0\n", "embedded null"),
        (b"\xff", "utf-8"),
        (b"." + b" " * 4096, "is longer than 4096 bytes"),
    ],
)
def test_invalid_common_directory_is_only_a_finding(
    checkout_paths: tuple[Path, Path], content: bytes, reason: str
) -> None:
    source, workspace = checkout_paths
    (source / ".git/commondir").write_bytes(content)
    findings: list[str] = []
    assert not worktree_writes.is_main_checkout(source, findings)
    assert len(findings) == 1
    assert findings[0].startswith(f"cannot audit main checkout {source}: ")
    assert reason in findings[0]
    assert not worktree_writes.is_main_checkout(source)
    assert findings[0] in worktree_writes.validate_writes(
        workspace, source, (str(source),)
    )


def test_common_directory_at_byte_limit_is_still_audited(
    checkout_paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source, workspace = checkout_paths
    (source / ".git/commondir").write_bytes(b"." + b" " * 4095)
    bounded_file_reads(monkeypatch, 4097)
    findings: list[str] = []
    assert worktree_writes.is_main_checkout(source, findings)
    assert findings == []
    assert (
        f"write root {source} reaches main checkout {source}"
        in worktree_writes.validate_writes(workspace, source, (str(source),))
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 directories")
def test_unreadable_child_directory_is_an_audit_finding(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    source, workspace = checkout_paths
    root = tmp_path / "writable"
    blocked = root / "unreadable"
    blocked.mkdir(parents=True)
    blocked.chmod(0)
    try:
        findings = worktree_writes.validate_writes(workspace, source, (str(root),))
    finally:
        blocked.chmod(0o700)
    assert any(
        line.startswith(f"cannot audit write root {root}: cannot read {blocked}: ")
        for line in findings
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode 000 directories")
@pytest.mark.parametrize("entry", [".", ".git"])
def test_unreadable_git_directory_is_an_audit_finding(
    checkout_paths: tuple[Path, Path], entry: str
) -> None:
    source, workspace = checkout_paths
    blocked = source / entry
    blocked.chmod(0)
    try:
        findings: list[str] = []
        assert not worktree_writes.is_main_checkout(source, findings)
        assert len(findings) == 1
        assert findings[0].startswith(f"cannot audit main checkout {source}: ")
        assert findings[0] in worktree_writes.validate_writes(
            workspace, source, (str(source),)
        )
    finally:
        blocked.chmod(0o700)


def test_git_directory_under_a_file_is_no_checkout(tmp_path: Path) -> None:
    path = tmp_path / "ordinary-file"
    path.touch()
    findings: list[str] = []
    assert not worktree_writes.is_main_checkout(path, findings)
    assert findings == []


def test_a_one_byte_head_is_a_main_checkout(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x")
    assert worktree_writes.is_main_checkout(tmp_path)


def test_commondir_opens_without_following_or_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / ".git" / "commondir").write_text(".")
    opened: list[int] = []
    real_open = os.open

    def record(path: Path, flags: int) -> int:
        opened.append(flags)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", record)
    assert worktree_writes.is_main_checkout(tmp_path)
    assert opened == [os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK]


def contained_findings(checkout_paths: tuple[Path, Path], root: Path) -> list[str]:
    source, workspace = checkout_paths
    found = worktree_writes.validate_writes(workspace, source, (str(root),))
    return [line for line in found if str(root) in line]


def test_contained_scan_reports_a_nested_checkout_it_cannot_audit(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    root = tmp_path / "shared"
    (root / "inner" / ".git" / "HEAD").mkdir(parents=True)
    head = root / "inner" / ".git" / "HEAD"
    message = (
        f"cannot audit main checkout {root / 'inner'}: {head} is not a regular file"
    )
    assert contained_findings(checkout_paths, root) == [message]


def test_contained_scan_stops_below_the_scan_depth(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    root = tmp_path / "shared"
    at_limit = root.joinpath(*"abc")
    beyond = root.joinpath(*"def", "g")
    for checkout in (at_limit, beyond):
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert worktree_writes.CHECKOUT_SCAN_DEPTH == 3
    assert contained_findings(checkout_paths, root) == [
        f"write root {root} contains main checkout {at_limit}"
    ]


def test_contained_scan_never_descends_into_a_git_directory(
    tmp_path: Path, checkout_paths: tuple[Path, Path]
) -> None:
    root = tmp_path / "shared"
    inner = root / "a" / ".git" / "inner"
    (inner / ".git").mkdir(parents=True)
    (inner / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    assert contained_findings(checkout_paths, root) == []


def test_a_write_root_over_the_own_workspace_is_not_a_finding(
    checkout_paths: tuple[Path, Path],
) -> None:
    source, workspace = checkout_paths
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/agent\n")
    found = worktree_writes.validate_writes(workspace, source, (str(workspace.parent),))
    assert not any(f"contains main checkout {workspace}" in line for line in found)
