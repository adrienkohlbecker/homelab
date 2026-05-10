#!/usr/bin/env python3
"""One-shot migration: relabel ansible-vault entries with explicit vault-ids
(prod / test) and move minio_users from host_vars into group_vars.

PRECONDITIONS
=============
1. New keychain entries / pass files are set up so `./vault.sh prod` and
   `./vault.sh test` both succeed:
     macOS:
       security add-generic-password -a ak \\
         -j "vault password ansible (prod)" -s homelab-vault-prod -w
       security add-generic-password -a ak \\
         -j "vault password ansible (test)" -s homelab-vault-test -w
     Linux:
       mv ~/.config/homelab/vault-pass{,-prod}
       install -m 0400 /dev/stdin ~/.config/homelab/vault-pass-test <<<"$NEW_PW"
2. The prod password equals the current single password (so existing
   ciphertext decrypts on the first pass). The test password is new.

WHAT IT DOES
============
1. Moves `minio_users` from host_vars/box.yml -> group_vars/test.yml
   and from host_vars/lab.yml -> group_vars/prod.yml. The vault blobs
   inside are treated as opaque text during the move and re-encrypted
   in step 2.
2. Walks every yaml under group_vars/ and host_vars/. For each !vault
   block: decrypts with the current secret, re-encrypts with a labeled
   secret -- `test` for files in TEST_FILES, `prod` for everything else.
3. Rewrites ansible.cfg's `vault_password_file = vault.sh` line to
   `vault_identity_list = prod@vault.sh, test@vault.sh` plus
   `vault_id_match = True`.

The script is rerunnable: re-running re-encrypts (which is harmless
besides churn) and the minio_users move is gated on the source still
holding the block.

Reverse with: `git checkout -- group_vars/ host_vars/ ansible.cfg`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ansible.parsing.vault import VaultLib, VaultSecret

REPO = Path(__file__).resolve().parent.parent

# Files whose !vault entries should land under the `test` vault-id.
# Everything else gets `prod`.
TEST_FILES = {
    REPO / "group_vars/test.yml",
    REPO / "host_vars/box.yml",
}

# Match a single `!vault |` block plus its indented ciphertext lines
# (envelope header + hex chunks). Captures the indentation prefix so we
# can re-emit with the same indent.
VAULT_RE = re.compile(
    r"(?P<tag>!vault[ \t]*\|[ \t]*\n)"
    r"(?P<blob>(?P<indent>[ \t]+)\$ANSIBLE_VAULT;[^\n]*\n"
    r"(?:(?P=indent)[0-9A-Fa-f]+\n)+)"
)


def vault_password(vault_id: str) -> bytes:
    out = subprocess.run(
        [str(REPO / "vault.sh"), vault_id],
        check=True,
        capture_output=True,
    )
    return out.stdout.rstrip(b"\n")


def reencrypt_text(text: str, vault: VaultLib, target_id: str, target_secret: VaultSecret) -> str:
    """Replace each !vault block with a re-encrypted version under target_id."""

    def repl(m: re.Match[str]) -> str:
        blob = m.group("blob")
        indent = m.group("indent")
        # Strip indent off each line to get the canonical vault envelope
        ciphertext = "\n".join(line[len(indent):] for line in blob.splitlines()).encode()
        plaintext = vault.decrypt(ciphertext)
        new_ct = vault.encrypt(
            plaintext,
            secret=target_secret,
            vault_id=target_id,
        ).decode()
        new_blob = "\n".join(indent + line for line in new_ct.splitlines() if line) + "\n"
        return m.group("tag") + new_blob

    return VAULT_RE.sub(repl, text)


def cut_minio_users(src_path: Path) -> tuple[str, str] | None:
    """Cut the `minio_users:` block from src_path. Returns (block, remaining_text),
    or None if no minio_users block is present (idempotent re-run)."""
    text = src_path.read_text()
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.startswith("minio_users:"):
            start = i
            break
    if start is None:
        return None
    # End at the first line that's at column 0 and isn't blank/comment.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if line and not line[0].isspace() and not line.startswith("#"):
            end = j
            break
    block_lines = lines[start:end]
    while block_lines and block_lines[-1].strip() == "":
        block_lines.pop()
    block = "\n".join(block_lines) + "\n"
    new_lines = lines[:start] + lines[end:]
    new = "\n".join(new_lines)
    new = re.sub(r"\n{3,}", "\n\n", new)
    return block, new


def append_block(dst_path: Path, block: str) -> None:
    text = dst_path.read_text()
    if not text.endswith("\n"):
        text += "\n"
    if not text.endswith("\n\n"):
        text += "\n"
    dst_path.write_text(text + block.rstrip("\n") + "\n")


def update_ansible_cfg() -> None:
    cfg = REPO / "ansible.cfg"
    text = cfg.read_text()
    if "vault_identity_list" in text:
        return
    new_text, n = re.subn(
        r"^vault_password_file\s*=\s*vault\.sh\s*$",
        "vault_identity_list = prod@vault.sh, test@vault.sh\nvault_id_match = True",
        text,
        count=1,
        flags=re.M,
    )
    if n != 1:
        raise SystemExit("ansible.cfg: couldn't find `vault_password_file = vault.sh` line")
    cfg.write_text(new_text)


def move_minio_users(src: Path, dst: Path, label: str) -> None:
    cut = cut_minio_users(src)
    if cut is None:
        return
    block, remaining = cut
    src.write_text(remaining)
    append_block(dst, block)
    print(f"  moved minio_users: {src.relative_to(REPO)} -> {dst.relative_to(REPO)} ({label})")


def main() -> int:
    prod_pw = vault_password("prod")
    test_pw = vault_password("test")
    if not prod_pw or not test_pw:
        print("vault.sh returned an empty password for prod or test", file=sys.stderr)
        return 1

    prod_secret = VaultSecret(prod_pw)
    test_secret = VaultSecret(test_pw)
    # `default` lets us decrypt existing un-labeled (1.1) entries during
    # the first pass. After migration every entry carries an explicit
    # label and `default` becomes inert.
    vault = VaultLib(
        [
            (b"prod", prod_secret),
            (b"test", test_secret),
            (b"default", prod_secret),
        ]
    )

    print("step 1: moving minio_users into group_vars")
    move_minio_users(REPO / "host_vars/box.yml", REPO / "group_vars/test.yml", "test")
    move_minio_users(REPO / "host_vars/lab.yml", REPO / "group_vars/prod.yml", "prod")

    print("step 2: relabeling !vault entries")
    yamls = sorted(REPO.glob("group_vars/*.yml")) + sorted(REPO.glob("host_vars/*.yml"))
    for yml in yamls:
        text = yml.read_text()
        if "!vault" not in text:
            continue
        target_id = "test" if yml.resolve() in TEST_FILES else "prod"
        target_secret = test_secret if target_id == "test" else prod_secret
        new_text = reencrypt_text(text, vault, target_id, target_secret)
        if new_text != text:
            yml.write_text(new_text)
            print(f"  relabeled {yml.relative_to(REPO)} -> id={target_id}")

    print("step 3: updating ansible.cfg")
    update_ansible_cfg()

    print("\ndone. review with `git diff` and commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
