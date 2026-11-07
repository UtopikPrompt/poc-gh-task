# Inference Model Runtime Infrastructure

This note describes the local-rendered infrastructure added under
`inference-platform/kubernetes`. It does not apply resources, prepare host
storage, create Secrets, download models, or verify a live cluster.

## Downloader

The single-replica `inference-model-download-worker` uses the existing
Codefactory LangGraph image and its installed `model-download-worker` entrypoint.
It runs in namespace `inference`, on `k8s-node01`, with supplemental GID
`20000` and `umask 0002`. Only this workload mounts
`inference-model-artifacts` writable. The GPU and CPU inference containers keep
their read-only mounts.

Before enabling it, prepare the already-provisioned PV directory
`/mnt/k8s-local-nvme/inference-models` as a real directory owned by
group GID `20000`, with setgid and group-write permissions. Do not use a
privileged init container to change host ownership. The worker's
`MODEL_DIRECTORY` is `/storage`, the PVC mount root. The inference mount path is
`/models`, and each `--model` argument must point to `/models/<filename>`. Both
paths refer to the same PVC root; no artifact copy or nested artifact directory
is used.

Create Secret `inference-model-downloader-credentials` in namespace `inference`
with key `LANGGRAPH_DB_PASSWORD`, using the approved Secret-management process.
The non-secret ConfigMap connects to
`codefactory-postgres.codefactory.svc.cluster.local:5432` as
`codefactory_langgraph` with `sslmode=disable`, matching the current dev
database configuration. Do not copy secret values into manifests or this
document. The worker waits for the 009/010 tables before starting; the
Codefactory migration Job now copies those migrations from the LangGraph image.

Its probes only confirm that the process is alive; they do not prove queue or
database health. The worker has no Kubernetes API token or deployment
permissions.

## Metrics

Prometheus `v3.5.0` scrapes the kubelet cAdvisor endpoint through the Kubernetes
API server proxy, limited to node `k8s-node01` and the two inference pods. Its
ClusterRole can only get the named `nodes/proxy` subresource for `k8s-node01`;
it cannot mutate cluster resources or discover other Nodes. A recording rule derives the CPU-limit series expected by
the existing metrics API from cAdvisor CPU quota/period samples.

The NVIDIA DCGM exporter uses the verified upstream image
`nvcr.io/nvidia/k8s/dcgm-exporter:4.2.3-4.1.3-ubuntu22.04`, Kubernetes
pod-resource labeling, and the node's `/var/lib/kubelet/pod-resources` socket.
It is restricted to `k8s-node01`, has no Kubernetes API token, and uses the
upstream-required root UID and `SYS_ADMIN` capability. It requires the node's
NVIDIA container runtime/driver integration to expose GPU telemetry; verify
that integration and the pod-resources socket before applying. It requests no
GPU resource, so it does not reserve a device from inference.

The scrape jobs retain only container CPU/RAM and DCGM utilization/framebuffer
metrics. The Codefactory service queries Prometheus through the internal
ClusterIP `prometheus.inference.svc.cluster.local:9090`; there is no Ingress,
TLS, or Prometheus authentication in this bundle. Restrict in-cluster access
according to the cluster's network policy. Prometheus stores seven days of
time series in a bounded `emptyDir`, so its history is lost if the pod is
replaced. The UI/API reports inference-container CPU and working-set RAM, not
whole-node CPU or RAM. GPU metrics may be unavailable until DCGM can associate
GPU samples with the active GPU inference pod; unavailable samples are not
filled with zero.

## Model Rollout Reconciler

The single-replica `inference-model-rollout-reconciler` consumes both the
durable workload lifecycle outbox and the model rollout outbox. Workload events
create CPU/GPU Deployments and Services from the read-only generated template
ConfigMap, adopt only the two known defaults labeled `inference-serving`, and
delete only resources bearing the controller ownership labels. Model rollout
events set `--model` to the validated `/models/<filename>` path and acknowledge
success only after the Deployment's observed generation, updated replicas, and
available replicas match the patched generation. Workload failures are
retried with backoff and public status uses fixed safe text. Workload `ready`
means the Deployment has available replicas; model rollout `ready` means its
event was acknowledged. Neither independently verifies inference behavior.

The inference Deployment templates are sourced from the checked-in CPU/GPU
profile manifests, but Kustomize publishes them as ConfigMap data instead of
applying the resources directly. The portal and LangGraph stay in namespace
`codefactory`; inference Services, Deployments, artifacts, and the reconciler
stay in namespace `inference`. This keeps control-plane ownership separate
from inference resources and avoids a later Kustomize apply recreating a
workload deleted through the portal.

The reconciler uses the Codefactory LangGraph image and its
`model-rollout-reconciler` entrypoint. Its Kubernetes permissions are limited
to get/create/patch/delete on Deployments and Services and get on Deployment
status in namespace `inference`; it has no list or cluster-wide permissions.
The controller derives resource names from validated instance IDs and checks
ownership labels before mutation or deletion. It has no model volume, PVC,
Pod, Node, or Secret access. The downloader remains without a Kubernetes
service account token or workload permissions.

Before enabling either workload, provide the same random
`MODEL_RECONCILER_TOKEN` through the approved Secret manager: key
`MODEL_RECONCILER_TOKEN` in `codefactory-runtime-secrets` in namespace
`codefactory` for LangGraph, and key `MODEL_RECONCILER_TOKEN` in Secret
`inference-model-reconciler-credentials` in namespace `inference` for the
reconciler. Do not put its value in a ConfigMap or source control. The token is
distinct from `INTERNAL_SERVICE_TOKEN`. The reconciler ConfigMap points to
`http://langgraph-service.codefactory.svc.cluster.local:8001`; ensure that
internal service DNS/network access is available. The configured 900-second
lease exceeds the 600-second rollout timeout so the reconciler has time to
acknowledge before the lease can be reclaimed.

The shared Codefactory migration Job defaults to the full migration sequence
for fresh databases. The dev environment selects migration 016 after migration
015; this profile does not replay earlier migrations. Apply the Codefactory
migrations and provide both Secret keys before enabling the reconciler. The
rendered manifests do not create or read Secret values, download models, or
apply resources to a cluster.

Render the combined infrastructure without changing a cluster:

```sh
kubectl kustomize inference-platform/kubernetes
```