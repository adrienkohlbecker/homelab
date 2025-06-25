FROM ubuntu:22.04

RUN set -x \
 && export DEBIAN_FRONTEND=noninteractive \
 && apt-get update \
 && apt-get install -y openssh-server systemd \
 && rm -f /etc/ssh/ssh_host* \
 && rm -rf /var/lib/apt/lists/*

COPY ssh-keygen.service /etc/systemd/system/ssh-keygen.service
RUN systemctl enable ssh-keygen.service

RUN set -x \
 && mkdir -p /root/.ssh \
 && echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIN1YdxBpNlzxDqfJyw/QKow1F+wvG9hXGoqiysfJOn5Y vagrant insecure public key" > /root/.ssh/authorized_keys \
 && chmod 0700 /root/.ssh \
 && chmod 0600 /root/.ssh/authorized_keys

# python3-apt: must be installed to use check mode. If run normally this module can auto-install it.
# python3-requests: to use docker module in check mode
# sudo: needed to use become_user
# cron: Failed to find required executable "crontab" in paths:
RUN set -x \
 && export DEBIAN_FRONTEND=noninteractive \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3-apt \
 && apt-get install -y --no-install-recommends python3-requests \
 && apt-get install -y --no-install-recommends sudo \
 && apt-get install -y --no-install-recommends cron \
 && rm -rf /var/lib/apt/lists/*

# Ensure we have a working apt cache (keep the cleanup in other sections for easy reuse)
RUN apt-get update

# to use podman-in-podman and docker-in-podman
VOLUME /var/lib/containers
VOLUME /var/lib/docker

EXPOSE 22

CMD [ "/lib/systemd/systemd" ]
