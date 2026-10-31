# Inference Model Storage: Infrastructure Operator Note

This infrastructure-owned configuration reserves a static local PV for model
artifacts on the verified `/mnt/k8s-local-nvme` ext4 filesystem on
`k8s-node01`. It does not provision the host directory, install artifacts, or
start a workload.

## Consumer contract

- Namespace: `inference`.
- StorageClass: `inference-models-local`.
- PVC: `inference-model-artifacts` in the `inference` namespace.
- Workloads must mount that PVC read-only by setting `readOnly: true` on the
  container's `volumeMount`. The workload selects its own container mount path;
  this infrastructure bundle does not define one.

## Before applying

- Confirm `/mnt/k8s-local-nvme` is still the expected ext4 NVMe mount on
  `k8s-node01` and is mounted persistently across node restarts. The configured
  PV path is the dedicated subdirectory
  `/mnt/k8s-local-nvme/inference-models`, not the mount root.
- An operator must create that directory on the node before the PV can be used.
  Set its owner to `root`, its group to a deployment-selected inference-model
  group, and its mode to `0750`. No numeric GID is defined by this repository;
  choose one for the deployment and configure the future pod's
  `supplementalGroups` to match. Do not use a privileged init container to
  change host permissions. Preserve the existing mount root's `root:root`
  ownership and `0700` mode; do not broaden its permissions.
- Confirm the directory's owner, group, and mode, and that no other PV, PVC, or
  StorageClass has been created with these names. The manifest creates the
  `inference` namespace; the future inference pod and PVC must use that
  namespace.

Read-only checks for the node and prepared directory:

```sh
findmnt --target /mnt/k8s-local-nvme
stat -c '%U:%G %a %n' /mnt/k8s-local-nvme/inference-models
```

Review the resource definitions without persisting them:

```sh
kubectl kustomize kubernetes/infrastructure/overlays/k8s-node01/inference-storage
kubectl apply --dry-run=client -k kubernetes/infrastructure/overlays/k8s-node01/inference-storage
```

## Capacity and runtime use

The matching PV and PVC request 500Gi, a conservative reserved claim against
the verified 1.3 TB filesystem that leaves substantial headroom. Local PV
capacity is scheduling metadata, not a filesystem quota; monitor actual use
and preserve free space for filesystem overhead and node operation. The
`Retain` reclaim policy preserves the backing data when the PVC is deleted;
released-volume cleanup or reuse requires an explicit operator decision.

The directory may be empty. This configuration makes no claim that a model
artifact exists or that the volume is already provisioned. When a workload is
separately approved, mount the PVC named in the consumer contract read-only and
grant its runtime only the selected group read access. Do not add a writable or
privileged permission-fixing init container.