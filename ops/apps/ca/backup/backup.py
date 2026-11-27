#!/usr/bin/env python3
"""Back up the staged CA project; requires Python 3.11+ and OpenSSL."""

# ruff: noqa: S603, T201
import argparse
import hashlib
import re
import subprocess  # nosec B404 - fixed argv commands, no shell
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

# cspell:ignore arcname
MEMBERS = ("db", "config", "roots", "templates", "compose.yaml")
DOCKER = "/usr/local/bin/docker"
CONTAINER = "ca-fixture-ca-1"
# The compose healthcheck runs every 30 s, so the first healthy status can take
# that long: allow 24 polls x 5 s = 120 s per start attempt.
HEALTH_POLLS = 24
HEALTH_INTERVAL = 5.0
START_ATTEMPTS = 2


def archive_name(now: datetime) -> str:
    return now.astimezone(UTC).strftime("ca-%Y%m%dT%H%M%S%fZ.tar.gz.enc")


def run(command: list[str], project: Path) -> None:
    # Commands use argument lists; paths never pass through a shell.
    subprocess.run(command, cwd=project, check=True, capture_output=True)  # nosec B603


def healthy() -> bool:
    # The container's own healthcheck is `step ca health`, the CA's /health.
    result = subprocess.run(  # nosec B603
        [
            DOCKER,
            "inspect",
            "--format",
            "{{.State.Running}} {{.State.Health.Status}}",
            CONTAINER,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == "true healthy"


def restart(compose: list[str], project: Path) -> None:
    """Start the CA and wait for it to be healthy, retrying the start once."""
    for _ in range(START_ATTEMPTS):
        try:
            run([*compose, "start", "step-ca"], project)
        except subprocess.CalledProcessError:
            continue
        for _ in range(HEALTH_POLLS):
            if healthy():
                return
            time.sleep(HEALTH_INTERVAL)
    message = "step-ca is not healthy after restart; the CA may be down"
    raise RuntimeError(message)


def retain(destination: Path) -> None:
    archives = sorted(
        path
        for path in destination.glob("ca-*.tar.gz.enc")
        if re.fullmatch(r"ca-\d{8}T\d{12}Z\.tar\.gz\.enc", path.name)
    )
    for path in archives[:-7]:
        path.with_suffix(path.suffix + ".sha256").unlink()
        path.unlink()


def check_key(key: Path) -> None:
    metadata = key.stat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        message = "key file must be owned by root with no group or other permissions"
        raise ValueError(message)


def backup(project: Path, destination: Path, key: Path) -> Path:
    check_key(key)
    # Require the pre-mounted destination: never silently back up to a new local dir.
    if not destination.is_dir():
        message = (
            "destination must be an existing directory on the mounted backup share"
        )
        raise ValueError(message)
    target = destination / archive_name(datetime.now(UTC))
    compose = ["/usr/local/bin/docker-compose", "-p", "ca"]
    with tempfile.TemporaryDirectory(prefix="ca-backup-") as scratch:
        plaintext = Path(scratch) / "ca.tar.gz"
        encrypted = Path(scratch) / "ca.tar.gz.enc"
        try:
            run([*compose, "stop", "step-ca"], project)
            with tarfile.open(plaintext, "w:gz") as archive:
                for member in MEMBERS:
                    archive.add(project / member, arcname=member)
        finally:
            restart(compose, project)
        run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-in",
                str(plaintext),
                "-out",
                str(encrypted),
                "-pass",
                f"file:{key}",
            ],
            project,
        )
        # Copy only encrypted bytes to the share, publishing after the copy completes.
        pending = target.with_suffix(".partial")
        pending.write_bytes(encrypted.read_bytes())
        pending.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(
        f"{digest}  {target.name}\n", encoding="ascii"
    )
    retain(destination)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("/srv/example-storage/docker/ca"))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        backup(args.project, args.destination, args.key_file)
    except (
        OSError,
        RuntimeError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        print(f"CA backup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
