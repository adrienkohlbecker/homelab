FROM ubuntu:22.04

RUN set -x \
 && export DEBIAN_FRONTEND=noninteractive \
 && apt-get update \
 && apt-get install -y openssh-server \
 && rm -f /etc/ssh/ssh_host* \
 && mkdir /var/run/sshd \
 && rm -rf /var/lib/apt/lists/*

RUN set -x \
 && mkdir /root/.ssh \
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
 && apt-get install -y python3-apt \
 && apt-get install -y python3-requests \
 && apt-get install -y sudo \
 && apt-get install -y  cron

EXPOSE 22

COPY entry.sh /entry.sh
ENTRYPOINT ["/entry.sh"]

CMD ["/usr/sbin/sshd", "-D", "-e"]
