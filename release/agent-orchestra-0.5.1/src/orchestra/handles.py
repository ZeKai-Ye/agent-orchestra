"""Handle registry: one SSH public key per human/agent handle.

A handle exists so a claim can be *verified*, not merely asserted: `handles/<handle>.pub`
is turned into a git allowed_signers file, and every claim commit must carry a signature
from that key. Revocation is deleting the file (docs/12-join.md).
"""

from __future__ import annotations

import re
from pathlib import Path

HANDLE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
HANDLES_DIR = Path("handles")


class HandleError(RuntimeError):
    pass


def path_for(repo: Path, handle: str) -> Path:
    if not HANDLE_PATTERN.match(handle):
        raise HandleError(f"invalid handle: {handle!r} (lowercase letters, digits, dash; 2-31 chars)")
    return Path(repo) / HANDLES_DIR / f"{handle}.pub"


def register(repo: Path, handle: str, public_key: str) -> Path:
    text = public_key.strip()
    if not text.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise HandleError("expected an OpenSSH public key (ssh-ed25519 ...)")
    path = path_for(repo, handle)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def exists(repo: Path, handle: str) -> bool:
    return path_for(repo, handle).exists()


def list_handles(repo: Path) -> list[str]:
    directory = Path(repo) / HANDLES_DIR
    if not directory.exists():
        return []
    return sorted(key.stem for key in directory.glob("*.pub"))