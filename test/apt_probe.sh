#!/usr/bin/env bash
# Temporary measurement: of the ~4.8 minutes the site converge spends in apt,
# how much is fetching bytes and how much is dpkg unpacking them? A caching
# proxy only helps the first. Installs the site test's heaviest package sets
# twice: once download-only into a clean cache, then again from that cache.
# Runs on a disposable CI host, which it deliberately pollutes.
set -euo pipefail

# The package sets behind the slowest apt tasks in the site converge.
sets=(
  "libvirt-daemon-system libvirt-clients virtinst"
  "kdump-tools crash kexec-tools makedumpfile"
  "postfix libsasl2-modules bsd-mailx"
  "samba samba-common-bin"
  "python3-debian"
  "chrony"
)

total_fetch=0
total_unpack=0

elapsed() { date +%s.%N; }

for pkgs in "${sets[@]}"; do
  read -r -a pkg_list <<<"$pkgs"

  # A clean cache makes the download real rather than a no-op.
  sudo rm -rf /var/cache/apt/archives/*.deb
  start=$(elapsed)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -d "${pkg_list[@]}" >/dev/null 2>&1 || true
  fetch=$(echo "$(elapsed) - $start" | bc)

  # Everything needed is cached now, so this second pass is unpack + configure.
  start=$(elapsed)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkg_list[@]}" >/dev/null 2>&1 || true
  unpack=$(echo "$(elapsed) - $start" | bc)

  printf '%-56s fetch %6.2fs   unpack %6.2fs\n' "$pkgs" "$fetch" "$unpack"
  total_fetch=$(echo "$total_fetch + $fetch" | bc)
  total_unpack=$(echo "$total_unpack + $unpack" | bc)
done

echo "---"
printf 'TOTAL fetch %.2fs   unpack %.2fs\n' "$total_fetch" "$total_unpack"
python3 -c "
f, u = $total_fetch, $total_unpack
print(f'fetch is {100 * f / (f + u):.0f}% of apt time; a cache can only address that share')
"
