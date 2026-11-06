# Inference Model Download and Metrics Contract

## Ownership and endpoints

The LangGraph service owns each configured inference row's desired model configuration, durable PostgreSQL download queue and rollout outbox, and Prometheus metric projection. Instance IDs are `gpu`, `cpu`, or a lowercase DNS-style ID prefixed with `gpu-` or `cpu-` (for example, `gpu-secondary` or `cpu-secondary`). Each non-default ID must have a matching Deployment named `llama-cpp-server-{instance_id}` in namespace `inference`; the existing defaults map to `llama-cpp-server` and `llama-cpp-server-cpu`. Instance rows and matching workloads are provisioned outside the portal; this API edits existing rows and does not create Kubernetes workloads. The management portal is the public boundary: model URL/configuration reads, download submission, download status, and rollout status are limited to a signed GitHub Organization Owner session. Writes also require same-origin HTTPS. Portal-to-service endpoints require `INTERNAL_SERVICE_TOKEN`; no browser talks directly to LangGraph or PostgreSQL.

The portal exposes:

- `GET /api/inference-instances`: owner-only desired model filename and rollout status for every configured instance row.
- `GET /api/inference-models`: owner-only list of regular `.gguf` files currently present in shared model artifact storage. The list is the authoritative shared inventory, not configured filenames or download history.
- `PUT /api/inference-instances/{instance_id}` applies a filename from the current inventory to that instance, including when re-requesting a rollout for the unchanged filename; a configured filename that has gone missing remains visible but cannot be applied until an available file is selected.
- `POST /api/inference-model-downloads`: owner-only global enqueue; adds a verified GGUF to shared storage without selecting a CPU/GPU model or starting a rollout.
- `GET /api/inference-model-downloads/{request_id}`: owner-only durable status. URL, SHA-256, transport exception text, and credentials are omitted.
- Applying a downloaded catalog file is a separate per-instance `Apply model` action. It updates that instance's desired filename and queues a durable rollout; it is not part of download submission.
- `GET /api/inference-instances` also returns separate `rollout_status`, `rollout_request_id`, and fixed `rollout_error` fields. Global download `ready` means the artifact is published; rollout `ready` means a rollout event was acknowledged.
- `GET /api/inference-metrics`: authenticated organization member view of CPU, RAM, GPU utilization, VRAM, and per-field availability state.

Equivalent bearer-authenticated service routes are specified in `contracts/openapi/langgraph-service.json`; portal routes are in `contracts/openapi/management-portal.json`.

## Available artifact inventory

`inference-model-artifact-api` runs in the `inference` namespace beside the `inference-model-artifacts` PVC. It mounts `/storage` read-only and returns filenames only; it does not fetch files, follow symlinks, expose file contents, or treat configured-but-absent names as available. Its bearer-protected `/internal/models/available` endpoint is proxied through LangGraph's internal management API, then exposed to the browser only through the portal's verified-owner route. The API reuses the dedicated `MODEL_RECONCILER_TOKEN` value, but supports only listing and has no write operation. The LangGraph and artifact API deployments must receive the same reconciler token value.

## Durable download worker

Install the LangGraph service package and run this exact process entrypoint:

```sh
model-download-worker
```

Required environment:

- `LANGGRAPH_DB_HOST`
- `LANGGRAPH_DB_PORT`
- `LANGGRAPH_DB_NAME`
- `LANGGRAPH_DB_USERNAME`
- `LANGGRAPH_DB_PASSWORD`
- `LANGGRAPH_DB_SSLMODE`
- `MODEL_DIRECTORY`: the writable artifact directory, not the PVC mount root

Optional worker controls are `MODEL_DOWNLOAD_POLL_SECONDS` (default `5`) and `MODEL_DOWNLOAD_LEASE_SECONDS` (default `120`, minimum `30`). The worker uses the same LangGraph database role/configuration as the service. It atomically claims one request with `FOR UPDATE SKIP LOCKED`; a UUID lease prevents another worker from completing or failing that claim. Expired leases are reclaimed. One active request per GPU/CPU instance is allowed.

The worker status contract is `queued -> downloading -> verifying -> ready` or `failed`. A lease-expired `downloading` or `verifying` request may be reclaimed as `downloading`. A failed request can be retried by submitting a new request after it is terminal. Only one active shared-storage request is allowed globally. Fixed public errors are used so URL parameters and transport exception details are never returned.

## Durable rollout handoff

Migrations `011_inference_model_rollouts.sql` and `012_multiple_inference_instances.sql` add the versioned rollout outbox and support additional prefixed instance IDs. Shared download completion does not select an instance or create a rollout. Migration `013_global_model_downloads.sql` supersedes pending/leased events created by the former instance-coupled download flow and resets its in-progress per-instance download/rollout indicators. Migration `014_inference_model_apply.sql` decouples rollout requests from download records; each accepted per-instance PUT atomically saves the desired filename and creates a rollout event, including when reapplying the unchanged filename after `not_requested` or `failed`.

Run the reconciler as a separate process/workload. Set `MODEL_RECONCILER_TOKEN` on the LangGraph service and inject the exact same secret value into the reconciler as its bearer token. This token is separate from `INTERNAL_SERVICE_TOKEN`; the reconciler needs no portal credential. The reconciler calls these endpoints over the internal service network:

Reconciler environment:

- `LANGGRAPH_SERVICE_URL` (required): internal LangGraph service base URL, such as `http://langgraph-service:8000`; endpoint paths below are appended without a trailing slash.
- `MODEL_RECONCILER_TOKEN` (required): dedicated bearer secret, identical to the LangGraph service setting and distinct from `INTERNAL_SERVICE_TOKEN`.
- `MODEL_ROLLOUT_POLL_SECONDS` (optional, default `5`): delay after a claim returns no event or a retryable transport error; must be positive.
- `MODEL_ROLLOUT_LEASE_SECONDS` (optional, default `120`): claim lease requested on each claim; integer range `30` through `900`. The reconciler must finish and acknowledge before `lease_until`; otherwise the service makes the event claimable again.

`POST /internal/reconciler/inference-model-rollouts/claim`

Request:

```json
{"lease_seconds": 120}
```

`lease_seconds` is optional (default `120`, range `30` through `900`). No work returns `{"event": null}`. A claimed event returns:

```json
{
	"event": {
		"event_id": "<uuid>",
		"instance_id": "gpu",
		"rollout_request_id": "<uuid>",
		"model_filename": "approved-model.gguf",
		"config_version": 7,
		"idempotency_key": "gpu:7:<download-request-uuid>",
		"lease_token": "<uuid>",
		"lease_until": "<RFC3339 timestamp>",
		"attempts": 1
	}
}
```

Instance IDs must be `gpu`, `cpu`, or a valid `gpu-*` / `cpu-*` ID. The filename is the owner-approved basename, not a path or URL. Use `idempotency_key` as the idempotency key for the per-instance Deployment update. Claims are at-least-once: expired leases are reclaimed with a new lease token and the same event/idempotency key. Before delivery, the service matches the event's download request ID, filename, and config version against the current ready configuration; old events are discarded as `superseded`. While an event is pending or leased, portal edits and another download request for that instance are rejected with `409`.

After the Deployment update succeeds or fails, acknowledge with:

`POST /internal/reconciler/inference-model-rollouts/{event_id}/ack`

```json
{"lease_token": "<uuid from claim>", "outcome": "succeeded"}
```

`outcome` is exactly `succeeded` or `failed`. A failed acknowledgment records only the fixed owner-safe message `Model rollout failed; verify the inference deployment and retry.`; the endpoint does not accept or persist internal exception text, credentials, or Kubernetes details. Response:

```json
{"event_id": "<uuid>", "status": "succeeded"}
```

The status may also be `failed` or `superseded`. Repeating the same terminal acknowledgment with the same lease token returns the same status. A stale/expired lease returns `409`; an unknown event returns `404`. Both endpoints require `Authorization: Bearer $MODEL_RECONCILER_TOKEN`. Missing token configuration returns `503`; incorrect credentials return `401`.

The portal's rollout status is one of `not_requested`, `waiting_for_download`, `pending`, `rolling_out`, `ready`, or `failed`; it is not the download status. A download failure never generates a rollout event. Successful rollout status means the reconciler acknowledged the update, not that Kubernetes health checks or model inference were independently verified.

The reconciler Role can get and patch Deployments in the `inference` namespace so it can target any configured instance Deployment by name. This namespace-scoped permission is broader than an explicit two-name allowlist; keep unrelated workloads out of this namespace. It does not need write access to the model PVC. The downloader remains without Kubernetes credentials or patch permissions.

The worker process must have effective supplemental group GID `20000`. `MODEL_DIRECTORY` must be a real directory on the prepared model volume, group-owned by GID 20000, setgid, and group-writable. The worker refuses to publish unless its temporary file inherits GID 20000; final artifacts are mode `0640`. Temporary files are created inside that same directory, SHA-256 is checked before `os.replace`, and the directory is synced after publication. Inference containers continue to mount the model volume read-only. This contract does not change or deploy any Kubernetes workload.

## URL, network, and artifact policy

Requests require a safe `.gguf` basename supplied as owner-selected model metadata; it is never taken from a redirect's `Location` header. The submitted URL must be HTTPS on exactly `huggingface.co`, use the default HTTPS port, and have no userinfo, fragment, or credential-like query keys. Before enqueueing, the service resolves the exact requested file from Hugging Face model-tree metadata and requires a valid LFS SHA-256; the durable request stores that digest and an immutable `resolve` URL. The worker still verifies the downloaded bytes against the digest before publishing.

The worker disables environment proxies and follows at most five HTTPS redirects, limited to `huggingface.co` and its Hugging Face CDN domains (`*.huggingface.co` and `*.hf.co`). Before each connection it resolves the destination, rejects every non-global/reserved/private/loopback/link-local/multicast/unspecified answer, and connects to one of the checked public IPs while retaining TLS hostname verification. A 30-minute overall deadline and bounded socket timeouts apply. The body is streamed with identity encoding, and both `Content-Length` and actual streamed bytes are capped at 100 GiB. The digest is verified before an atomic publish; partial files are removed on every failure. No credentials or raw exception messages are included in public status.

## Metrics mapping

Set `PROMETHEUS_URL` to the Prometheus-compatible API base URL. The service uses `/api/v1/query` and queries the configured `inference_instance` label for each row. Default IDs use existing Deployment names; additional IDs use pods from `llama-cpp-server-gpu-*` or `llama-cpp-server-cpu-*` Deployments in namespace `inference`:

- CPU utilization is container CPU rate divided by its Kubernetes CPU limit, expressed as percent.
- RAM is `container_memory_working_set_bytes` summed for the inference container.
- GPU utilization and framebuffer used/free are `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_DEV_FB_USED`, and `DCGM_FI_DEV_FB_FREE`; DCGM samples must carry `namespace` and `pod` labels matching the inference pod.
- VRAM bytes are calculated from DCGM MiB samples.

Each metric includes a nullable value and `available`, `stale`, or `unavailable` state. Samples older than 120 seconds are marked stale; absent configuration, failed queries, or absent series are unavailable and never synthesized as zero. GPU fields for CPU-prefixed instances are unavailable. The UI displays CPU and GPU utilization together, and RAM and VRAM used/total together, while preserving each underlying sample state. `sampled_at` is the newest returned sample timestamp, or null when no values are available.

The API and UI define the contract only. Prometheus scraping, DCGM deployment/configuration, inference pod relabeling, and workload reconciliation remain a separate infrastructure step.
