#!/usr/bin/env bash
#MISE description="Install QEMU with the HVF PSCI CPU_ON fix through the local homelab/qemu Homebrew tap, so ZFSBootMenu can kexec with several vCPUs on Apple Silicon"
set -euo pipefail

# Homebrew only installs formulae from taps, so mirror the repo formula into a
# local tap that carries no git remote.
repo=$(git rev-parse --show-toplevel)
tap_dir="$(brew --repository)/Library/Taps/homelab/homebrew-qemu"

mkdir -p "${tap_dir}/Formula"
cp "${repo}/packer/homebrew/Formula/qemu-hvf.rb" "${tap_dir}/Formula/qemu-hvf.rb"

brew install --build-from-source homelab/qemu/qemu-hvf

echo "installed; put $(brew --prefix qemu-hvf)/bin ahead of Homebrew's bin on PATH to use it"
