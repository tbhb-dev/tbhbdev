FROM ubuntu:26.04@sha256:d66b366dac61b575bee2aef07e9c26e8070096fbe84a3d4d3eb0da069b5d76db AS base

ENV container=docker
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-apt systemd systemd-sysv dbus sudo ca-certificates \
        apparmor nftables iproute2 aptitude rsync acl dbus-user-session e2fsprogs \
        iptables passt uidmap curl jq openssl \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d
RUN systemctl mask systemd-modules-load.service getty@tty1.service
CMD ["/sbin/init"]

FROM base AS ops_accounts
# An ops-ro account as the lab nodes had it, which the role removes.
RUN useradd --create-home --shell /bin/bash --groups adm ops-ro

FROM base AS docker_rootless
RUN mkdir -p /etc/apt/keyrings \
    && curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo '1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570  /etc/apt/keyrings/docker.asc' | sha256sum --check \
    && printf 'deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n' > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker-ce docker-ce-cli docker-ce-rootless-extras containerd.io \
    && rm /etc/apt/keyrings/docker.asc /etc/apt/sources.list.d/docker.list \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops

FROM docker_rootless AS actions_runner_pool
RUN curl --fail --silent --show-error https://downloads.1password.com/linux/keys/1password.asc -o /etc/apt/keyrings/1password.asc \
    && echo 'f39e7dd9dedc581ced85732832f217e0de5860a3b80279b5af4bc7c6d8157bae  /etc/apt/keyrings/1password.asc' | sha256sum --check \
    && printf 'deb [arch=%s signed-by=/etc/apt/keyrings/1password.asc] https://downloads.1password.com/linux/debian/%s stable main\n' "$(dpkg --print-architecture)" "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/1password.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends 1password-cli \
    && rm /etc/apt/keyrings/1password.asc /etc/apt/sources.list.d/1password.list \
    && rm -rf /var/lib/apt/lists/*

FROM docker_rootless AS ops_ssh
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d \
    && printf 'PasswordAuthentication yes\n' > /etc/ssh/sshd_config.d/50-cloud-init.conf \
    && systemctl disable ssh.service \
    && systemctl enable nftables.service ssh.socket dbus.socket

FROM base AS docker_engine
RUN mkdir -p /etc/apt/keyrings \
    && curl --fail --silent --show-error https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo '1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570  /etc/apt/keyrings/docker.asc' | sha256sum --check \
    && curl --fail --silent --show-error https://gvisor.dev/archive.key -o /etc/apt/keyrings/gvisor.asc \
    && echo '757d2ebdf00cdc5661f88443e2bea848d3e2ded0780cbcd2d811be05e0c1d5dd  /etc/apt/keyrings/gvisor.asc' | sha256sum --check \
    && printf 'deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu resolute stable\n' > /etc/apt/sources.list.d/docker.list \
    && printf 'deb [signed-by=/etc/apt/keyrings/gvisor.asc] https://storage.googleapis.com/gvisor/releases release main\n' > /etc/apt/sources.list.d/gvisor.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin runsc \
    && rm /etc/apt/keyrings/docker.asc /etc/apt/keyrings/gvisor.asc /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/gvisor.list \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops

FROM base AS kvm_host
# A lab node has a package index, which the role's check run needs to
# resolve the KVM packages. The base image drops it.
RUN apt-get update
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops

FROM base AS nas_python
# An unprivileged ops with no sudo, as on a NAS. The base image already has
# curl, tar and sha256sum, and sh is dash, so the script stays POSIX.
RUN useradd --create-home --shell /bin/sh ops

FROM base AS agent_account
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

FROM base AS agent_host
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update

FROM base AS codex_cli

FROM base AS claude_code

FROM base AS ops_tbhbdev

FROM base AS connect_token

FROM base AS agent_workspace
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

FROM base AS agent_harness
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git golang-go \
    && git clone --quiet --depth 1 --branch v2.73.3 https://github.com/twpayne/chezmoi /tmp/chezmoi \
    && (cd /tmp/chezmoi && GOPROXY=https://proxy.golang.org GOSUMDB=sum.golang.org \
        go build -o /usr/local/bin/chezmoi .) \
    && rm -rf /tmp/chezmoi /root/go /root/.cache /var/lib/apt/lists/*

FROM base AS agent_signing
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

FROM base AS lab_agent
# The groups docker_engine and kvm_host leave on a lab node.
RUN groupadd --force --system docker \
    && groupadd --force --system libvirt \
    && groupadd --force --system kvm

FROM base AS lab_grub
# A lab node's /etc/default/grub.d, and a stand-in for update-grub, which
# can't probe disks in a container. It counts its runs for verify.
RUN mkdir -p /etc/default/grub.d \
    && printf '#!/bin/sh\nn=$(cat /var/lib/lab-grub-update-count 2>/dev/null || echo 0)\necho $((n + 1)) > /var/lib/lab-grub-update-count\n' > /usr/sbin/update-grub \
    && chmod +x /usr/sbin/update-grub

FROM base AS host_audit
# auditd as a lab node would have it, installed without starting: a
# container can't load audit rules into the kernel it shares.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends auditd \
    && rm -rf /var/lib/apt/lists/* \
    && rm /usr/sbin/policy-rc.d \
    && systemctl disable auditd.service

FROM base AS host_hardening

FROM base AS host_telemetry
# A lab node has a package index, which the role's install needs. The role
# adds Grafana's repository and installs Alloy itself.
RUN apt-get update

FROM base AS bazel_server_reaper

FROM base AS mac_snapshot_thin

FROM base AS ci_vm
# A lab node has a package index, which the role's install needs.
# Use HTTPS so package downloads work on runners that cannot reach HTTP mirrors.
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops

FROM base AS chrony_nts
# The role requires the lab host's existing chrony installation.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chrony

FROM agent_workspace AS event_gateway_deploy
RUN useradd --create-home --shell /bin/bash ops \
    && printf 'ops ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/ops \
    && chmod 0440 /etc/sudoers.d/ops
