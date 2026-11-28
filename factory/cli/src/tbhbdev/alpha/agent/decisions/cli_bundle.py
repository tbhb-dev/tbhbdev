"""Pack a CLI build and its dependency pins into a deterministic tar."""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
from typing import TYPE_CHECKING

from tbhbdev.alpha.agent.model import Invalid

if TYPE_CHECKING:
    from collections.abc import Mapping


def pack(
    package_files: Mapping[str, bytes],
    dist_info_files: Mapping[str, bytes],
    *,
    identity: str,
    distributions: Mapping[str, str],
) -> tuple[bytes, str]:
    """Return the tar bytes and SHA-256 for files relative to the import root."""
    if re.fullmatch(r"[0-9a-f]{40}", identity) is None:
        message = "CLI bundle requires a 40-hex build commit"
        raise Invalid(message)
    pins = "".join(
        f"{name}=={version}\n"
        for name, version in sorted(distributions.items())
        if name.casefold() != "tbhbdev"
    )
    entries = {**package_files, **dist_info_files, "requirements.txt": pins.encode()}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", encoding="utf-8") as archive:
        for name, content in sorted(entries.items()):
            if name.endswith(".pyc") or "__pycache__" in name.split("/"):
                continue
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    bundle = buffer.getvalue()
    return bundle, hashlib.sha256(bundle).hexdigest()
