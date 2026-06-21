#!/usr/bin/env bash
set -euo pipefail

# Per-source staging directory created by packer.
: "${BUILD_DIR:?}"
# Disk suffix to apply to shipped images: raw or qcow2.
: "${IMAGE_FORMAT:?}"
# Whether the harness can boot-test this source image.
: "${QEMU_TEST_IMAGE:?}"
# Harness machine spec used for qemu boot verification.
: "${MACHINE:?}"
# Ubuntu release name passed through to the harness.
: "${UBUNTU_NAME:?}"
# Path to the qemu launch helper.
: "${LAUNCH_PY:?}"
# Whether finalized artifacts should be published.
: "${PUBLISH:?}"
# Path to the atomic publish helper.
: "${PUBLISH_PY:?}"
# Lock file shared with tests that consume published artifacts.
: "${PUBLISH_LOCK:?}"
# Final per-source artifact directory.
: "${OUTPUT_DIR:?}"

# packer-ubuntu is the residual cloud-image OS disk. provision.sh installs onto
# packer-ubuntu-1..N, so nothing downstream consumes it.
rm -f "$BUILD_DIR/packer-ubuntu"

shopt -s nullglob
disks=("$BUILD_DIR"/packer-ubuntu-*)
[ "${#disks[@]}" -gt 0 ] || {
  echo "postprocess.sh: no built disks found in $BUILD_DIR" >&2
  exit 1
}

for disk in "${disks[@]}"; do
  mv "$disk" "$disk.$IMAGE_FORMAT"
done

if [ "$QEMU_TEST_IMAGE" = "true" ]; then
  "$LAUNCH_PY" \
    --machine "$MACHINE" \
    --ubuntu "$UBUNTU_NAME" \
    --timeout 300 \
    --exit-after-ready \
    --image-dir "$BUILD_DIR"
fi

if [ "$IMAGE_FORMAT" != "raw" ]; then
  for disk in "$BUILD_DIR"/packer-ubuntu-*.qcow2; do
    echo "==> compressing $(basename "$disk")"
    qemu-img convert -W -c -O qcow2 -o compression_type=zstd "$disk" "$disk.tmp"
    mv "$disk.tmp" "$disk"
  done
fi

if [ "$PUBLISH" != "true" ]; then
  echo "==> Skipping publish (publish=false)"
  exit 0
fi

python3 "$PUBLISH_PY" "$PUBLISH_LOCK" "$BUILD_DIR" "$OUTPUT_DIR"
