#!/usr/bin/env bash
set -euo pipefail

: "${BUILD_DIR:?}"
: "${IMAGE_FORMAT:?}"
: "${QEMU_TEST_IMAGE:?}"
: "${MACHINE:?}"
: "${UBUNTU_NAME:?}"
: "${LAUNCH_PY:?}"
: "${PUBLISH:?}"
: "${PUBLISH_PY:?}"
: "${PUBLISH_LOCK:?}"
: "${OUTPUT_DIR:?}"

build_dir=$BUILD_DIR
image_format=$IMAGE_FORMAT
qemu_test_image=$QEMU_TEST_IMAGE
machine=$MACHINE
ubuntu=$UBUNTU_NAME
launch_py=$LAUNCH_PY
publish=$PUBLISH
publish_py=$PUBLISH_PY
publish_lock=$PUBLISH_LOCK
output_dir=$OUTPUT_DIR

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
  mv "$disk" "$disk.$image_format"
done

if [ "$qemu_test_image" = "true" ]; then
  "$launch_py" \
    --machine "$machine" \
    --ubuntu "$ubuntu" \
    --timeout 300 \
    --exit-after-ready \
    --image-dir "$build_dir"
fi

if [ "$image_format" != "raw" ]; then
  for disk in "$build_dir"/packer-ubuntu-*.qcow2; do
    echo "==> compressing $(basename "$disk")"
    qemu-img convert -W -c -O qcow2 -o compression_type=zstd "$disk" "$disk.tmp"
    mv "$disk.tmp" "$disk"
  done
fi

if [ "$publish" != "true" ]; then
  echo "==> Skipping publish (publish=false)"
  exit 0
fi

python3 "$publish_py" "$publish_lock" "$build_dir" "$output_dir"
