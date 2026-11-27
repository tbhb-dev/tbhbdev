---
type: Readme
title: Host hardening
description: Allow guest DNS and address assignment on every configured virtual network bridge.
tags: [ansible, libvirt, firewall]
status: stable
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:06e0bdca59ec1c72
    at: "2026-11-12T05:32:37Z"
---

<!-- cspell:words virsh -->

Guest DNS and Dynamic Host Configuration Protocol (DHCP) traffic enter through the bridges in `host_hardening_libvirt_bridges` and `host_hardening_lab_bridge`. By default, the `libvirt` list contains one entry, [the default bridge](defaults/main.yml). Each name must match `^[A-Za-z0-9_.-]+\Z`.

On host-a, `inventory/host_vars/host-a.yml` defines `o11y_bridge` once. Both `o11y_vm_network_bridge` and `host_hardening_libvirt_bridges` read it, so the observability network and the firewall use the same bridge.

Before writing the firewall, the role reads all defined networks with `virsh --connect=qemu:///system net-list --all --name` and reads each bridge with `virsh --connect=qemu:///system net-info`. A bridge outside `host_hardening_libvirt_bridges` that differs from `host_hardening_lab_bridge` fails the run with its name and the configuration to update. Networks without a `Bridge:` line skip the assertion. These reads and the assertion also run in check mode. Hosts without `virsh` skip the network check. Run the role with privilege escalation, as the lab hardening playbook does.
