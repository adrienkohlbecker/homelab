#!/usr/bin/env bash
set -euo pipefail

# Root staging directory containing one subdirectory per source.
: "${BUILD_DIRECTORY:?}"
# Packer source name, used as the staging and publish subdirectory.
: "${SOURCE_NAME:?}"
# Disk suffix to apply to shipped images: raw or qcow2.
: "${IMAGE_FORMAT:?}"
# Shipped image target: qemu images are harness-verified.
: "${IMAGE_TARGET:?}"
# Ubuntu release name passed through to the harness.
: "${UBUNTU_NAME:?}"
# Whether finalized artifacts should be published.
: "${PUBLISH:?}"
# Parent directory for published source artifacts.
: "${OUTPUT_DIRECTORY:?}"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
build_dir="$BUILD_DIRECTORY/$SOURCE_NAME"

# packer-ubuntu is the residual cloud-image OS disk. provision.sh installs onto
# packer-ubuntu-1..N, so nothing downstream consumes it.
rm -f "$build_dir/packer-ubuntu"

shopt -s nullglob
disks=("$build_dir"/packer-ubuntu-*)
[ "${#disks[@]}" -gt 0 ] || {
  echo "postprocess.sh: no built disks found in $build_dir" >&2
  exit 1
}

for disk in "${disks[@]}"; do
  mv "$disk" "$disk.$IMAGE_FORMAT"
done

if [ "$IMAGE_TARGET" = "qemu" ]; then
  "$script_dir/../../test/launch.py" \
    --machine "$SOURCE_NAME" \
    --ubuntu "$UBUNTU_NAME" \
    --timeout 300 \
    --exit-after-ready \
    --image-dir "$build_dir"
fi

if [ "$IMAGE_FORMAT" != "raw" ]; then
  for disk in "$build_dir"/packer-ubuntu-*.qcow2; do
    echo "==> compressing $(basename "$disk")"
    qemu-img convert -W -c -O qcow2 -o compression_type=zstd "$disk" "$disk.tmp"
    mv "$disk.tmp" "$disk"
  done
fi

if [ "$PUBLISH" != "true" ]; then
  echo "==> Skipping publish (publish=false)"
  exit 0
fi

"$script_dir/../publish.py" "$OUTPUT_DIRECTORY/.publish-lock" "$build_dir" "$OUTPUT_DIRECTORY/$SOURCE_NAME"
