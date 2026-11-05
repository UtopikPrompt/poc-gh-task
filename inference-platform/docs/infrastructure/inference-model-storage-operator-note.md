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
- The downloader writes model files directly beneath the PV path. Its container
  uses `MODEL_DIRECTORY=/storage`; inference containers mount the same PVC
  read-only at `/models` and load from `/models/<filename>`.
- The downloader runs in namespace `inference`, is pinned to `k8s-node01`, and
  requires supplemental group GID `20000`. Prepare the `artifacts` directory
  with the ownership, setgid, and group-write permissions described below; the
  workload intentionally has no permission-fixing init container.

## Before applying

- Confirm `/mnt/k8s-local-nvme` is still the expected ext4 NVMe mount on
  `k8s-node01` and is mounted persistently across node restarts. The configured
  PV path is the dedicated subdirectory
  `/mnt/k8s-local-nvme/inference-models`, not the mount root.
- The node must have the dedicated `inference-model-downloaders` group at GID
  `20000`. Set the mount root to `root:inference-model-downloaders`, with group
  read/write/execute and setgid (`2770`). Set existing directories/files to
  owner `root`, group `inference-model-downloaders`, with group-write access and
  setgid on directories. Leave existing UIDs and symlink inodes unchanged. A downloader
  must use supplemental GID `20000` and `umask 0002` so new artifacts inherit
  the group and remain group-writable. Do not use a privileged init container
  to change host permissions.
- Every inference pod using this PVC must set
  `spec.securityContext.supplementalGroups: [20000]` for model read access, and
  every inference container must mount the PVC with `readOnly: true`. A
  separately approved downloader must also use supplemental GID `20000` to
  write artifacts. Do not make inference mounts writable.
- Confirm the directory's owner, group, and mode, and that no other PV, PVC, or
  StorageClass has been created with these names. The manifest creates the
  `inference` namespace; the future inference pod and PVC must use that
  namespace.

Read-only checks for the node and prepared directory:

```sh
findmnt --target /mnt/k8s-local-nvme
stat -c '%U:%G %a %n' /mnt/k8s-local-nvme /mnt/k8s-local-nvme/inference-models
```

Review the resource definitions without persisting them:

```sh
kubectl kustomize inference-platform/kubernetes/infrastructure/overlays/k8s-node01/inference-storage
kubectl apply --dry-run=client -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/inference-storage
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