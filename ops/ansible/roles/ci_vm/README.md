---
type: Readme
title: The CI VM role
description: Run a CI runner VM on a lab node with a fixed address on the hypervisor's translated network and a forwarding rule.
tags: [ansible, libvirt, ci]
status: draft
generated:
  - role: generated
    harness: codex:0.164.0
    model: openai:gpt-6.1-sol
    effort: high
    surface: exec
    session: sha-256-64:96dd6ad6aeb6fc4c
    at: "2026-11-10T14:09:53Z"
---

<!-- cspell:words nftables virsh -->

`ci_vm` runs one `libvirt` domain, `<node>-ci`, on a lab node. `playbooks/lab-ci-vms.yml` applies it to the `lab` group, after `kvm_host` has installed `libvirt`. The VM attaches to `default`, the Network Address Translation (NAT) network. A raw logical volume provides the disk. A cloud-init seed creates the `ops` account and installs `Tailscale`. An `nftables` table drops the virtual machine's forwarded traffic to private, shared, link-local and unique local destinations.

## Fixed address

`ci_vm_address` is the virtual machine's fixed IPv4 address on the `libvirt` network. Each lab node has its own copy of the network. The fixed address is the same on each of the three isolated networks. After the domain exists, the role uses `virsh` to read its hardware address on `ci_vm_network` and the network's Dynamic Host Configuration Protocol (DHCP) host entries. It then runs `virsh net-update`:

- **No entry for the hardware address:** `add-last ip-dhcp-host`.
- **An entry for the hardware address with another address:** `modify ip-dhcp-host`.
- **An entry for the hardware address with the address:** nothing.

The update applies with `--live --config`. The update leaves an entry for another hardware address and the guest's current lease alone. A running VM takes the fixed address at its next DHCP renewal or reboot. Reboot the VM once after the first apply.

## How `Ansible` reaches the virtual machines

The `ci` inventory group connects to `ci_vm_address` through the lab node that hosts the VM, as `ops` on `host-example` and then on to the VM. The `Tailscale` policy denies the `Ansible` controllers every port on `tag:gh-runner`, so the virtual machines aren't reachable by their `Tailscale` names. `group_vars/ci.yml` sets `ansible_host` to the same address as `ci_vm_address`, because group variables can't see role defaults. It also repeats `ci_vm_network_gateway`. Both inventory values must agree with the role's settings, and the gateway must be a different IPv4 address in the virtual machine's /24. Traffic from a lab node to its own guest doesn't pass through the egress table's forward chain.

In `group_vars/all.yml`, the shared `ci_vm_controller_keys` variable validates and sorts `ssh_controller_keys` through `authorized_lines`, then replaces each source restriction with `ci_vm_network_gateway`. For the cloud-init seed, it resolves the role default on lab nodes at render time. The seed limits access to controller keys. VM-side controller lines restrict access to the lab node's gateway address. Anything on that lab node can offer a controller key. Controller names still resolve through the existing filter, which checks their membership in the private network.

## Sign the node key for a new VM

The `Tailscale` network requires signed node keys. A new VM joins with a new node key, and the network rejects the new node until a node with a trusted signing key signs that key. Until then the VM can't connect to any peer on the network, including 1Password Connect, where its runners read the GitHub App key. Each time the role creates the domain, including when someone destroys a VM and the role creates it again, @tbhb runs the `tailscale lock sign` command that `tailscale lock status` prints on the VM. Resizing or restarting the domain keeps its disk and node key, and doesn't need a new signature.

## Memory

The role reconciles the domain's persistent memory limits with `ci_vm_memory_mib`. It never restarts an existing VM. For a running VM, the new memory takes effect after the operator runs `virsh shutdown <node>-ci`, waits for it to stop, then runs `virsh start <node>-ci`. Never recreate the VM, because the network requires @tbhb to sign a new node key.

## Tests

The Molecule scenario runs in a container without the Kernel-based Virtual Machine (KVM), `libvirt`, or the Logical Volume Manager (LVM), with `ci_vm_hardware` false. It covers the egress table and the seed. Verification runs the memory tasks separately against a stateful `virsh` stub, covering equal memory, growth, shrinkage and check mode without changing a real domain. The fixed address has no Molecule coverage.
