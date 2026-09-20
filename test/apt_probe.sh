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

# The bake clears /var/lib/apt/lists, so without an index every install below
# would no-op instantly and the measurement would read as pure overhead.
sudo apt-get update -qq

for pkgs in "${sets[@]}"; do
  read -r -a pkg_list <<<"$pkgs"

  already=$(dpkg-query -W -f '${Status}\n' "${pkg_list[@]}" 2>/dev/null | grep -c "^install ok installed" || true)
  if [ "$already" -eq "${#pkg_list[@]}" ]; then
    echo "SKIP (already installed): $pkgs"
    continue
  fi

  # A clean cache makes the download real rather than a no-op.
  sudo rm -rf /var/cache/apt/archives/*.deb
  start=$(elapsed)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -d "${pkg_list[@]}" >/dev/null
  fetch=$(echo "$(elapsed) - $start" | bc)

  debs=$(find /var/cache/apt/archives -maxdepth 1 -name '*.deb' | wc -l)
  if [ "$debs" -eq 0 ]; then
    echo "ERROR: nothing downloaded for '$pkgs'; measurement would be meaningless" >&2
    exit 1
  fi

  # Everything needed is cached now, so this second pass is unpack + configure.
  start=$(elapsed)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkg_list[@]}" >/dev/null
  unpack=$(echo "$(elapsed) - $start" | bc)

  printf '%-52s %2s debs  fetch %6.2fs   unpack %6.2fs\n' "$pkgs" "$debs" "$fetch" "$unpack"
  total_fetch=$(echo "$total_fetch + $fetch" | bc)
  total_unpack=$(echo "$total_unpack + $unpack" | bc)
done

echo "---"
printf 'TOTAL fetch %.2fs   unpack %.2fs\n' "$total_fetch" "$total_unpack"
python3 -c "
f, u = $total_fetch, $total_unpack
print(f'fetch is {100 * f / (f + u):.0f}% of apt time; a cache can only address that share')
"
