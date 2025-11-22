from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyinfra.api.exceptions import PyinfraError
from pyinfra.context import host
from pyinfra.facts.files import Directory, File
from pyinfra.facts.server import Command, Home, LinuxDistribution
from pyinfra.operations import apt, files, server

from pyinfra_roles.helpers import file_put_with_validation

ROLE_ROOT = Path(__file__).resolve().parent
_PACKAGE_CACHE_TTL = 3600
_GPG_KEY_ID = "C248DE6357445D6302F9A62E74BFD03C20CC21AF"
_GPG_KEYSERVER = "keyserver.ubuntu.com"
_DOTFILES_REPO = "https://github.com/adrienkohlbecker/dotfiles.git"

_USER_PACKAGES = [
    "ccze",
    "curl",
    "git",
    "gparted",
    "gpg",
    "htop",
    "iotop",
    "iperf",
    "jq",
    "lsof",
    "man-db",
    "moreutils",
    "mosh",
    "ncdu",
    "nmon",
    "nnn",
    "nvme-cli",
    "progress",
    "screen",
    "socat",
    "stress",
    "sysstat",
    "telnet",
    "tig",
    "tmux",
    "tree",
    "usbutils",
    "vim",
    "zsh",
]


@dataclass
class Facts:
    """System facts gathered from the target host."""
    username: str
    needs_sudo: bool
    user_home: str
    ubuntu_version: str

    @classmethod
    def gather(cls) -> Facts:
        """Gather required facts from the target host."""
        username = host.get_fact(Command, "whoami")
        if not username:
            raise PyinfraError("Unable to determine username")

        user_home = host.get_fact(Home, username)
        if not user_home:
            raise PyinfraError("Unable to determine user_home")

        distribution = host.get_fact(LinuxDistribution) or {}
        ubuntu_version = distribution.get("release_meta", {}).get("VERSION_ID")
        if not ubuntu_version:
            raise PyinfraError("Unable to determine Ubuntu version")

        return cls(
            username=username,
            needs_sudo=username != "root",
            user_home=user_home,
            ubuntu_version=ubuntu_version,
        )


def apply() -> None:
    """
    Configure user environment with development tools and dotfiles.

    Sets up:
    - Essential CLI packages (git, zsh, monitoring tools, etc.)
    - zsh as default shell with authorized SSH keys
    - Password-less sudo access for non-root users
    - Git configuration with user email
    - GPG key import for commit signing
    - Dotfiles repository as bare git repo in ~/.dotfiles

    Requires host.data:
        ssh_public_keys: str - Primary SSH public keys (newline-separated)
        ssh_public_keys_additional: str (optional) - Additional SSH keys
        root_email: str - Email for git configuration
    """
    facts = Facts.gather()

    # Validate required host data
    authorized_keys = _build_authorized_keys(
        host.data.get("ssh_public_keys"),
        host.data.get("ssh_public_keys_additional"),
    )
    if not authorized_keys:
        raise PyinfraError("ssh_public_keys must contain at least one key")

    email = host.data.get("root_email")
    if not email:
        raise PyinfraError("user role requires root_email")

    # Install all required packages
    apt.packages(
        name="Install user packages and dependencies",
        packages=_USER_PACKAGES,
        update=True,
        cache_time=_PACKAGE_CACHE_TTL,
        _sudo=facts.needs_sudo,
    )

    # Configure user shell and SSH access
    server.user(
        name="Configure interactive user shell and keys",
        user=facts.username,
        home=facts.user_home,
        shell="/bin/zsh",
        public_keys=authorized_keys,
        delete_keys=True,  # Remove any keys not in our list
        _sudo=facts.needs_sudo,
    )

    # Enable password-less sudo for non-root users
    if facts.needs_sudo:
        file_put_with_validation(
            remote_path=f"/etc/sudoers.d/{facts.username}",
            content=f"{facts.username} ALL=(ALL) NOPASSWD:ALL\n",
            user="root",
            group="root",
            mode="0440",
            validate="visudo -cf",
            _sudo=True,
        )

    # Remove cloud-init's sudoers file to avoid conflicts
    files.file(
        name="Cleanup sudoers added by cloud-init",
        path="/etc/sudoers.d/90-cloud-init-users",
        present=False,
        _sudo=facts.needs_sudo,
    )

    # Configure git with user-specific settings
    files.template(
        name="Configure git",
        src=f"{ROLE_ROOT}/templates/gitconfig.j2",
        dest=f"{facts.user_home}/.gitconfig.local",
        user=facts.username,
        group=facts.username,
        mode="0644",
        root_email=email,
    )

    # Import GPG key for commit signing (idempotent check)
    if not host.get_fact(File, path=f"{facts.user_home}/.gnupg/pubring.kbx"):
        server.shell(
            name="Import GPG key for commit signing",
            commands=[f"gpg --keyserver {_GPG_KEYSERVER} --recv-keys {_GPG_KEY_ID}"],
            _chdir=facts.user_home,
        )

    # Set up dotfiles as a bare git repository
    dotfiles_dir = f"{facts.user_home}/.dotfiles"

    if not host.get_fact(Directory, path=dotfiles_dir):
        server.shell(
            name="Clone dotfiles as bare repository",
            commands=[f"git clone --bare {_DOTFILES_REPO} .dotfiles"],
            _chdir=facts.user_home,
        )

    # Check out dotfiles to home directory (only if not already checked out)
    if not host.get_fact(File, path=f"{facts.user_home}/.zshrc"):
        server.shell(
            name="Check out dotfiles to home directory",
            commands=[f"git --git-dir={dotfiles_dir} --work-tree={facts.user_home} reset --hard"],
            _chdir=facts.user_home,
        )

    # Ubuntu 22.04 doesn't support zdiff3 merge strategy
    if facts.ubuntu_version == "22.04":
        files.replace(
            name="Fix git config merge driver for Ubuntu 22.04",
            path=f"{facts.user_home}/.gitconfig",
            text="zdiff3",
            replace="diff3",
        )

    # Initialize dotfiles submodules (e.g., zsh themes)
    if not host.get_fact(File, path=f"{facts.user_home}/.zsh/pure/readme.md"):
        server.shell(
            name="Initialize dotfiles submodules",
            commands=[f"git --git-dir={dotfiles_dir} --work-tree={facts.user_home} submodule update --init --recursive"],
            _chdir=facts.user_home,
        )


def _build_authorized_keys(*key_blocks: str | None) -> list[str]:
    """
    Flatten and normalize SSH public keys from host data.

    Args:
        key_blocks: One or more newline-delimited strings of SSH public keys

    Returns:
        List of individual SSH public key strings, stripped and de-duplicated
    """
    keys: list[str] = []
    for block in key_blocks:
        if block:
            keys.extend(line.strip() for line in block.strip().splitlines() if line.strip())
    return keys
