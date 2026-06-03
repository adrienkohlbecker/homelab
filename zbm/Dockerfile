# Vendored, podman-compatible adaptation of zfsbootmenu's
# releng/docker/Dockerfile.
#
# Upstream uses BuildKit-only syntax (heredocs and --mount=type=cache),
# which Ubuntu's podman 4.9 / buildah 1.33 do not parse. This copy
# flattens heredocs into &&-chained RUNs and drops the build cache mount
# so the same Dockerfile can be built on both x86_64 and aarch64 hosts
# without docker/buildx. Keep in sync with upstream when bumping
# vars.zbm_version in mise.toml.
#
# Build context: zfsbootmenu/releng/docker (so build-init.sh is COPYable).

FROM ghcr.io/void-linux/void-glibc-full
LABEL org.opencontainers.image.authors="ZFSBootMenu Team, https://zfsbootmenu.org"

ARG XBPS_REPOS="https://repo-fastly.voidlinux.org/current"
ARG KERNELS="linux6.6 linux6.12 linux6.18"

RUN set -eu \
 && if [ -z "${KERNELS}" ]; then echo "ARG KERNELS must contain a value"; exit 1; fi \
 && mkdir -p /etc/xbps.d \
 && rm -f /etc/xbps.d/00-repository-main.conf \
 && for _repo in ${XBPS_REPOS}; do echo "repository=${_repo}" >> /etc/xbps.d/00-repository-main.conf; done \
 && xbps-install -Suy xbps \
 && xbps-install -Suy \
 && echo "ignorepkg=linux"         >  /etc/xbps.d/10-nolinux.conf \
 && echo "ignorepkg=linux-headers" >> /etc/xbps.d/10-nolinux.conf \
 && echo "noextract=/usr/libexec/mkinitcpio/*" >  /etc/xbps.d/15-noinitramfs.conf \
 && echo "noextract=/usr/libexec/dracut/*"     >> /etc/xbps.d/15-noinitramfs.conf \
 && kern_headers="" \
 && for _kern in ${KERNELS}; do kern_headers="${kern_headers} ${_kern}-headers"; done \
 && zbm_deps="$(xbps-query -Rp run_depends zfsbootmenu)" \
 && xbps-install -y ${KERNELS} ${kern_headers} ${zbm_deps} \
      zstd systemd-boot-efistub curl yq-go bash kbd mdadm \
      dracut mkinitcpio dracut-network gptfdisk iproute2 iputils parted \
      dosfstools e2fsprogs efibootmgr cryptsetup openssh util-linux kpartx \
 && echo "ignorepkg=dkms" > /etc/xbps.d/10-nodkms.conf \
 && xbps-pkgdb -m manual binutils \
 && xbps-remove -Roy dkms ${kern_headers} \
 && rm -rf /var/cache/xbps/*

ARG ZBM_COMMIT_HASH=
RUN [ -z "${ZBM_COMMIT_HASH}" ] || echo "${ZBM_COMMIT_HASH}" > /etc/zbm-commit-hash

ARG PACKAGES=
RUN [ -z "${PACKAGES}" ] || xbps-install -y ${PACKAGES}

RUN rm -f /var/cache/xbps/*

ARG ZBM_BUILDER=build-init.sh
COPY --chmod=755 ${ZBM_BUILDER} /
ENTRYPOINT [ "/build-init.sh" ]
