# llama.cpp GPU Smoke Test

This standalone smoke test runs a short llama-server inference on one Tesla P40. It uses the official prebuilt image and does not depend on Kubernetes, clone source, compile llama.cpp, or download a model. The selected local model directory is mounted read-only at `/models` and temporary logs are removed on exit.

## Pinned Image

- Image: `ghcr.io/ggml-org/llama.cpp@sha256:c1d664830580e1651c1cc8d3bbc161af474800a56a83226e6e22f94bb8ba1c59`
- Upstream package: b12019, `server-cuda12`
- Upstream image entrypoint: `/app/llama-server`; the script passes server arguments after the image name.
- The CUDA build includes Pascal `61-virtual` PTX, so a P40 uses PTX JIT rather than a native cubin. The CUDA 13 image is not suitable for Pascal.

## Prerequisites

Linux with an NVIDIA P40 reporting compute capability 6.1, a working NVIDIA driver, `nvidia-smi`, `curl`, and a Docker-compatible container CLI with NVIDIA GPU support. The default CLI is `docker`; set `CONTAINER_CLI` to another compatible executable if needed. The container runtime must be able to use the pinned image and expose the selected GPU.

Provide a readable, already-present local `.gguf` file through `MODEL_PATH`. `MODEL_SHA256` is optional and, when supplied, is checked before starting the container. GPU 0 is selected by default. The script never downloads a model or changes host model files.

## Pinned Smoke-Test Model

- Official repository: `Qwen/Qwen2.5-0.5B-Instruct-GGUF`
- Immutable revision: `9217f5db79a29953eb74d5343926648285ec7e67`
- File: `qwen2.5-0.5b-instruct-q4_k_m.gguf`
- SHA-256: `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`
- Size: `491400032` bytes
- License: Apache-2.0
- Official download URL: `https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf`

The staged path is `/mnt/k8s-local-nvme/inference-models/qwen2.5-0.5b-instruct-q4_k_m.gguf` on `k8s-node01`. The model directory is expected to exist on the verified NVMe mount, owned by `root:root` with mode `0755`; the mount-root parent is `root:root` with mode `0700`. Download to a temporary file outside this repository, verify the exact byte size and SHA-256 before installation, and install only the model file as `root:root` mode `0644` so the inference container can read it. Do not change either directory's ownership or mode. Set `MODEL_PATH` to the staged path and `MODEL_SHA256` to the hash above when invoking the smoke script.

## Run

From the repository root:

```sh
MODEL_PATH=/path/to/model.gguf bash inference-platform/inference/llama-cpp-smoke/run.sh
```

Optional settings are `MODEL_SHA256`, `CONTAINER_CLI`, `GPU_INDEX`, `GPU_LAYERS` (default 8), `CTX_SIZE` (default 1024), `MAX_TOKENS` (default 48, maximum 128), and `PORT` (default 18080). The script starts the pinned server image with `--n-gpu-layers`, sends one bounded deterministic completion request, samples host GPU memory every 250 ms, and removes the container and temporary files on exit.

## Success Criteria

The selected GPU must be a Tesla P40 with compute capability 6.1. The server must become ready, the completion request must succeed, and its log must show nonzero GPU layer offload. The script reports baseline and sampled peak GPU memory. Memory readings are device-wide and sampled, so unrelated GPU activity can affect them and brief peaks may be missed.

## Bounded Topology Benchmark (2026-10-31)

This is a lab comparison, not a production recommendation. The raw per-request timings and sampled GPU telemetry are in [benchmark-results-2026-10-31.json](benchmark-results-2026-10-31.json).

### Environment and Controls

- Node: Ubuntu 26.04.1 LTS, kernel `7.0.0-38-generic`; Kubernetes `v1.36.5+k3s1`, Kustomize `v5.8.1`, containerd `2.3.4-k3s1.36`.
- Runtime: llama.cpp build `b12019-1052f7e1d`, image `ghcr.io/ggml-org/llama.cpp@sha256:c1d664830580e1651c1cc8d3bbc161af474800a56a83226e6e22f94bb8ba1c59`.
- Model: Qwen2.5-0.5B-Instruct Q4_K_M, SHA-256 `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`. GGUF architecture metadata was not captured. The server reported context 2048 and A loaded 25/25 layers across CUDA0 and CUDA1.
- Hardware: two Tesla P40 GPUs, 23,040 MiB each, NVIDIA driver `580.178.04`; allocatable node resources were 80 CPU, `96127108Ki` memory, and 2 GPUs.
- Request: fixed `/completion` prompt below; 28 tokens from `/tokenize`; `n_predict=64`, `temperature=0`, `seed=42`, `stream=false`, and `cache_prompt=false`. Every measured request generated 64 tokens.
- Workload: two warmups per topology, then 3 windows of 10 requests for each concurrency (1 and 2). This is 30 measured requests per concurrency and 60 per topology. Each B pod also received direct warmups and one small preflight request at each concurrency; those were excluded. The runner used the Service ClusterIP from the node, not port-forwarding.
- Resource budget: A used one pod with requests 1500m CPU/1Gi memory and limits 2 CPU/2Gi, plus 2 GPUs. B used two pods, each with requests 750m/512Mi and limits 1 CPU/1Gi, plus 1 GPU each. Aggregate CPU and memory requests/limits were equal between topologies.

Prompt: “Explain why measuring a system matters. Give a clear answer in numbered steps, keep each step concise, and finish with one practical example.”

### Aggregate Results

Values are p50/p95 over 30 requests per concurrency. Percentiles use nearest-rank. Client latency is end-to-end; prompt and generation timings and generation tokens/s come from llama.cpp `timings`. Aggregate throughput is generated tokens divided by the combined wall time of the three windows.

| Topology | Concurrency | Client E2E ms | Prompt eval ms | Generation ms | Server generation tokens/s | Aggregate tokens/s | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| A: one instance, two GPUs | 1 | 315.6 / 319.0 | 10.46 / 12.51 | 301.81 / 304.16 | 208.7 / 209.3 | 202.49 | 0/30 |
| A: one instance, two GPUs | 2 | 347.7 / 368.4 | 14.51 / 15.43 | 305.77 / 308.46 | 206.0 / 207.8 | 366.89 | 0/30 |
| B: two instances, one GPU each | 1 | 330.9 / 334.3 | 12.18 / 13.19 | 315.55 / 319.32 | 199.6 / 203.4 | 193.42 | 0/30 |
| B: two instances, one GPU each | 2 | 344.6 / 393.6 | 11.05 / 19.18 | 314.80 / 337.09 | 196.9 / 202.7 | 367.12 | 0/30 |

### Measurement Windows

Each row is one 10-request window; timings are client E2E p50/p95 in milliseconds.

| Topology | Concurrency | Window | E2E p50/p95 ms | Aggregate tokens/s | Errors |
|---|---:|---:|---:|---:|---:|
| A | 1 | 1 | 316.0 / 319.3 | 202.0 | 0 |
| A | 1 | 2 | 315.2 / 319.0 | 202.5 | 0 |
| A | 1 | 3 | 315.2 / 316.4 | 203.0 | 0 |
| A | 2 | 1 | 348.4 / 369.8 | 365.5 | 0 |
| A | 2 | 2 | 347.8 / 352.5 | 366.8 | 0 |
| A | 2 | 3 | 346.7 / 350.3 | 368.3 | 0 |
| B | 1 | 1 | 330.4 / 334.2 | 193.4 | 0 |
| B | 1 | 2 | 330.4 / 334.3 | 193.4 | 0 |
| B | 1 | 3 | 331.2 / 336.6 | 193.4 | 0 |
| B | 2 | 1 | 349.4 / 398.8 | 362.6 | 0 |
| B | 2 | 2 | 347.7 / 376.9 | 362.4 | 0 |
| B | 2 | 3 | 336.0 / 355.1 | 376.7 | 0 |

### Telemetry and Outcome

| Topology/device | GPU UUID suffix | Max sampled GPU utilization | Max sampled VRAM | Container CPU/memory snapshot |
|---|---|---:|---:|---|
| A / GPU0 | `f5b42f9b-16dd-cfe9-aab5-9a6ca129c29d` | 41% | 525 MiB | 641m / 575Mi |
| A / GPU1 | `5cc04de7-1ef7-af52-1e2f-ca71fa8200e3` | 48% | 623 MiB | 641m / 575Mi |
| B / GPU0 | `f5b42f9b-16dd-cfe9-aab5-9a6ca129c29d` | 88% | 751 MiB | 603m / 530Mi |
| B / GPU1 | `5cc04de7-1ef7-af52-1e2f-ca71fa8200e3` | 87% | 751 MiB | 233m / 531Mi |

GPU telemetry was sampled every 500 ms (257 samples/device around A, including idle time; 32 samples/device during B). `kubectl top` values are point-in-time snapshots, not peaks. B's two pods each exposed a different GPU UUID, had the same image and model hash, and both received Service traffic: llama.cpp completion-log counts increased by 34 and 28, including the two Service warmups. The CPU-only Deployment remained 1/1 throughout.

B was about 4.5% lower in aggregate throughput at concurrency 1 and effectively tied at concurrency 2, while its concurrency-2 E2E p95 was higher. The 0.5B model is small relative to two P40s, so this workload does not establish a general topology winner. A's GPU sampler included idle intervals, and telemetry can miss short peaks. Requests were non-streaming, so TTFT was not measured. The first un-warmed request took about 7.9 seconds; warmups were excluded. Server logs reported prompt caching enabled globally while requests set `cache_prompt=false`; no server cache flags were changed. No production traffic was involved. Recreate rollouts caused brief planned GPU-Service interruptions; their duration was not measured.

The B measurement runner returned 60/60 successful requests per concurrency. A subsequent inline summary formatter had a Python syntax error, causing that shell command to exit 1 after the measurement; the EXIT trap restored Deployment revision 5, and the original root Kustomize composition was then reapplied and verified with an empty server diff. The raw JSON artifact preserves all measured request timings and telemetry.

Reproduce the measurement against a node-reachable llama.cpp API or ClusterIP with:

```sh
python3 inference-platform/inference/llama-cpp-smoke/benchmark.py \
	--base-url http://<reachable-service>:8080 \
	--windows 3 --requests 10 --warmups 2
```

## Qwen2.5-32B Topology Profiles (2026-10-31)

These profiles use the same official checkpoint and immutable revision, but different quantizations and context sizes. Both were measured in the bounded runs below. Any comparison is a profile/capacity comparison, not a pure topology or same-precision throughput comparison. Preserve the 0.5B results above; each 32B run has its own raw JSON artifact.

- Official repository: `Qwen/Qwen2.5-32B-Instruct-GGUF`
- Revision: `a15e3cc10f8bbb2c0af6f8f1f34a32e3b060c09`
- License: Apache-2.0
- Merge utility: `ghcr.io/ggml-org/llama.cpp@sha256:96bc9ac7094dade5fdec882416eab1eb99340b785a418bb701bd503647443ae7` (official linux/amd64 full-CUDA image; source tag b12019, commit `1052f7e1d62611d328ccc65d38c77cd194ee59fc`). The image entrypoint is overridden to `/app/llama-gguf-split`; upstream split files are merged with `--merge FIRST OUTPUT`.
- Staging directory: `/mnt/k8s-local-nvme/inference-models` on `k8s-node01`; source shards and merged files remain outside git.

| Candidate | Quantization | Merged file | Merged bytes | Merged SHA-256 | Topology/context profile |
|---|---|---|---:|---|---|
| A | Q8_0 | `qwen2.5-32b-instruct-q8_0.gguf` | 34,820,884,672 | `3f44bc82ae45da7d6d51fa7a0a3c9694f00c957b0118933b3818ee529ba28a4e` | One instance across two P40s, layer split `1,1`, context 16,384 |
| B | Q4_K_M | `qwen2.5-32b-instruct-q4_k_m.gguf` | 19,851,335,872 | `b00a1f91e34317fb9879ccba5f8adf52372079bb98c0e75420803022dd2de806` | Two replicas, one P40 each, context 4,096 per replica |

The upstream GGUF reader verified both merged files as `qwen2`, named `qwen2.5-32b-instruct`, with 64 blocks, embedding length 5,120, 40 attention heads, 8 KV heads, and metadata context length 131,072. Both configured contexts are below that metadata limit; the measured server logs reported all 65/65 layers offloaded and the requested context. Merged sizes are 960 bytes (Q8_0) and 512 bytes (Q4_K_M) smaller than their respective shard-byte sums because split metadata is consolidated during merging.

The node has two Tesla P40 GPUs with 23,040 MiB (22.5 GiB) each. From the verified architecture, a 128-element head dimension and 8 KV heads yield 262,144 bytes (256 KiB) of K+V cache per token per sequence if both caches use F16. That is 4 GiB at context 16,384, or about 2 GiB per GPU for an even two-GPU split; at context 4,096 it is 1 GiB per sequence. These are conditional estimates, not measured allocations; cache representation remains unverified. Weight quantization and KV-cache representation are separate settings. The merged weights are about 32.43 GiB for Q8_0 (about 16.21 GiB per GPU when evenly split) and 18.49 GiB for Q4_K_M on each replica's GPU. Device-wide measured VRAM is summarized below and includes all allocations, not just model weights or cache. Both contexts loaded successfully in the measured setup; this does not establish fit for higher context, concurrency, or other hardware. Do not infer fit from artifact size alone.

The Qwen card requires merging split GGUF files. Download each source shard to a temporary filename, verify its exact byte size and SHA-256, then atomically rename it. Keep the verified shards as well as merged files; do not use `--delete-splits`. The expected source-shard hashes are:

| Quantization | Shard | Bytes | SHA-256 |
|---|---|---:|---|
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00001-of-00009.gguf` | 3,941,956,768 | `a2020baa6f28f3c3783ab83ae725f6b6168b7c45ccc557cabdc691f9a4604eba` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00002-of-00009.gguf` | 3,927,757,760 | `ec6c0bb6b76013d00a0f9a57ca3b77e8ce8dd215cc5a3c13e66da7db1a386a9c` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00003-of-00009.gguf` | 3,994,654,144 | `f68b1c67d2f25c94ff26389b32d7dc6b91780984735e68c6cccda0bee4a3aefc` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00004-of-00009.gguf` | 3,994,654,144 | `377292038c5f89f00d78726f87ff84542140736b598ea5102ba508cc1ee80e16` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00005-of-00009.gguf` | 3,927,737,280 | `2d6c8b01140e81185a36dc88e655f5d6cc2068206e6ec26245bc5cc3845cb59b` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00006-of-00009.gguf` | 3,994,654,144 | `e70b8c7e09263c749eeb3513a0a3ae9eace8362ffc952bd3c0814814a270b7be` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00007-of-00009.gguf` | 3,994,654,144 | `7c609382c54100a2a750a11756bd32b731bb6a0004babc50dd6f69cd27b48daa` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00008-of-00009.gguf` | 3,927,737,280 | `2f33e61cf725ba37f114a56a16d5c1ea37f1467e89d804b5eb49a02620a303d6` |
| Q8_0 | `qwen2.5-32b-instruct-q8_0-00009-of-00009.gguf` | 3,117,079,968 | `82e154a199e9bfc4edad5dabb4359784c18aab1e9eaf3ce8ee23c994764422dc` |
| Q4_K_M | `qwen2.5-32b-instruct-q4_k_m-00001-of-00005.gguf` | 3,961,498,272 | `403434e5c845452c013661d586b97d5a53cf207462d180a07c301eafa9390d05` |
| Q4_K_M | `qwen2.5-32b-instruct-q4_k_m-00002-of-00005.gguf` | 3,948,996,064 | `7371e5c5d717a8c20f526dbfce5d3f201dc3f02140d1c836314cd0756e02e8d7` |
| Q4_K_M | `qwen2.5-32b-instruct-q4_k_m-00003-of-00005.gguf` | 3,993,478,688 | `f023ccc294c3bd3b2eac5b2dd40dea3aa4ca1d06c7460ead67c36386cd62e8fc` |
| Q4_K_M | `qwen2.5-32b-instruct-q4_k_m-00004-of-00005.gguf` | 3,950,347,744 | `05fe76d941454390cd7aa0de3b342f83a1d1226a959479af85bbfae7cd93e771` |
| Q4_K_M | `qwen2.5-32b-instruct-q4_k_m-00005-of-00005.gguf` | 3,997,015,616 | `8c2e8ecc686129c37821bd9f3b3e251e6ae80deadd3d3348f7e3e84492a63c24` |

### Bounded Qwen2.5-32B Results

Raw per-request measurements are preserved separately: [Q8 two-GPU profile](benchmark-results-2026-10-31-qwen2.5-32b-a-two-gpu-q8.json) and [Q4 two-replica profile](benchmark-results-2026-10-31-qwen2.5-32b-b-two-gpu-q4km.json).

Both runs used the pinned llama.cpp image and driver/runtime described above, the same prompt and Service ClusterIP `http://10.43.125.168:8080`, and the benchmark runner below. The prompt tokenized to 28 tokens. Each run used 2 warmups, then 3 windows of 10 requests at concurrency 1 and 2 (30 measured requests per concurrency); each completion requested and generated 64 tokens. Warmups and all measured requests had zero errors.

| Profile | Quantization/context | Replicas and allocation | Concurrency | Client E2E p50/p95 ms | Prompt eval p50/p95 ms | Generation p50/p95 ms | Server generation tok/s p50 | Aggregate tok/s | Errors |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| A | Q8_0 / 16,384 | 1; 8 CPU, 56 GiB, 2 GPUs | 1 | 10,114.0 / 10,117.1 | 223.2 / 226.0 | 9,886.3 / 9,889.3 | 6.37 | 6.33 | 0/30 |
| A | Q8_0 / 16,384 | 1; 8 CPU, 56 GiB, 2 GPUs | 2 | 10,701.5 / 10,720.9 | 369.0 / 370.3 | 10,246.5 / 10,250.8 | 6.15 | 11.96 | 0/30 |
| B | Q4_K_M / 4,096 | 2; each 4 CPU, 28 GiB, 1 GPU | 1 | 6,239.6 / 6,276.9 | 242.1 / 274.4 | 5,994.1 / 5,997.0 | 10.51 | 10.03 | 0/30 |
| B | Q4_K_M / 4,096 | 2; each 4 CPU, 28 GiB, 1 GPU | 2 | 6,790.0 / 6,946.9 | 240.0 / 386.2 | 6,392.4 / 6,546.6 | 9.85 | 19.33 | 0/30 |

Percentiles are nearest-rank across 30 successful requests. Client latency is end-to-end; prompt and generation durations and server generation speed come from llama.cpp `timings`. Aggregate throughput is generated tokens divided by the combined wall time of the three windows for that concurrency. These results show the measured profiles only; quantization and context changed along with replica topology, so they do not isolate a topology effect.

For B, the live Deployment was temporarily patched (no repository serving manifests changed) to 2 replicas with 4 CPU/28 GiB request=limit and one `nvidia.com/gpu` request=limit per replica. The NVIDIA device plugin exposed one distinct P40 UUID to each pod through `NVIDIA_VISIBLE_DEVICES`; both used explicit `--device CUDA0`, loaded 65/65 layers, reported `n_ctx=4096`, and passed direct completion preflights. No anti-affinity was needed. GPU sampling every 500 ms recorded 614 samples per device: GPU0 (`f5b42f9b-16dd-cfe9-aab5-9a6ca129c29d`) reached 100% utilization and used 20,057 MiB; GPU1 (`5cc04de7-1ef7-af52-1e2f-ca71fa8200e3`) reached 100% and used 20,057-20,059 MiB. Each device has 23,040 MiB total. For A, the recorded pre-benchmark loaded snapshot was 19,123 MiB on GPU0 and 18,705 MiB on GPU1; no concurrent A utilization/memory sampling was captured. Per-pod CPU/memory peaks during these runs were not recorded.

Reproduce the timing workload from the repository root with the currently running profile:

```sh
python3 inference-platform/inference/llama-cpp-smoke/benchmark.py \
	--base-url http://<reachable-service>:8080 \
	--windows 3 --requests 10 --warmups 2 --timeout 300
```

The original checked-in Kubernetes composition was reapplied after B. The GPU Deployment returned to one 0.5B Q4_K_M replica with two GPUs and the CPU-only Deployment remained 1/1. Neither run altered the 0.5B result artifact; staged model files remain outside git.

## Bounded Quantization Quality and Q8 16K Stress (2026-10-31)

The paired quality outputs and the separate Q8 long-context measurements are preserved in [Q8 matched-4K quality results](evaluation-results-2026-10-31-qwen2.5-32b-quality-q8-matched-4k.json), [Q4_K_M 4K quality results](evaluation-results-2026-10-31-qwen2.5-32b-quality-q4km.json), and [Q8 long-context stress results](evaluation-results-2026-10-31-qwen2.5-32b-long-context-q8.json). An additional exploratory Q8 quality pass at the active 16K setting is in [Q8 16K quality results](evaluation-results-2026-10-31-qwen2.5-32b-quality-q8.json); the context-matched comparison below uses the two 4K artifacts.

### Paired Quality Method and Results

Both quantizations used the same six prompts, explicit Qwen2.5-Instruct chat markers, `temperature=0`, `seed=42`, and `max_tokens=120`. Input lengths were identical for each pair: 80, 83, 63, 66, 88, and 71 tokens. Q8 ran on its normal two-P40 topology at a temporary 4K context; Q4_K_M ran on one visible P40 at 4K, with a temporary profile label. Model files are the verified same-revision artifacts documented above. This matches prompt and context, but not GPU placement or available VRAM.

| Task | Q8_0 / 4K | Q4_K_M / 4K | Objective outcome |
|---|---:|---:|---|
| Faithful extraction | Pass | Pass | Exact JSON fields and source values |
| Concise summary | Fail | Fail | Q8 omitted unchanged customer data; Q4 omitted the count of 48 services |
| Arithmetic | Fail | Fail | Both reached the 120-token cap before stating the final total |
| Instruction following | Fail | Fail | Q8 used `False` instead of the required `no`; Q4 did not follow the exact three-line format |
| Code explanation | Pass | Pass | Both gave the exact returned list and inclusive score threshold |
| Source-grounded facts | Pass | Pass | Both included all four required report facts |
| **Automated checks** | **3/6** | **3/6** | No subjective scoring performed |

The raw responses are retained; the automated checks do not replace human review, and this six-prompt set does not establish a general quality winner. The matched-context score is a quality-only comparison, not a speed result. The Q4 temporary Deployment used one requested GPU, 4 CPU, 28 GiB memory, `--device CUDA0`, and `--ctx-size 4096`; the assigned P40 exposed UUID `GPU-5cc04de7-1ef7-af52-1e2f-ca71fa8200e3`, and logs reported all 65/65 layers offloaded. Its sampled device peak was 20,059 MiB of 23,040 MiB, leaving 2,981 MiB (2.91 GiB) at the sampled peak. The Q8 4K counterpart retained the two-GPU `CUDA0,CUDA1` layer split.

The evaluation runner refuses to overwrite an existing output file. Example invocations, after the corresponding live profile is ready, are:

```sh
python3 inference-platform/inference/llama-cpp-smoke/evaluate_32b.py \
	--mode quality --profile q8-two-gpu-4k --timeout 300 \
	--output /tmp/q8-quality.json

python3 inference-platform/inference/llama-cpp-smoke/evaluate_32b.py \
	--mode quality --profile q4-one-gpu-4k --timeout 300 \
	--output /tmp/q4-quality.json
```

Q4 is a temporary live-only Deployment change; do not edit the checked-in serving manifests to retain it. Restore the source-defined active profile with `kubectl apply -k inference-platform/kubernetes`, wait for `kubectl -n inference rollout status deployment/llama-cpp-server`, then verify `kubectl diff -k inference-platform/kubernetes` is empty. The CPU Deployment is separate and remains untouched.

### Q8 Long-Context and Concurrency Method

The active Q8 profile used one pod, 8 CPU, 56 GiB memory, both Tesla P40s, layer split `1,1`, context 16,384, and the pinned llama.cpp `b12019-1052f7e1d` image. The server reported four slots sharing a 16,384-cell unified F16 KV cache of 4,096 MiB. Prompts used the same explicit chat template, placed retrieval facts at the beginning, middle, and end, and were tuned with `/tokenize`. Each serial request reserved 96 output tokens; both concurrent requests reserved 64 tokens each. The completion endpoint's `prompt_n` matched `/tokenize` for every request.

| Serial target | Actual prompt tokens | Prompt eval ms | Generation ms | Client latency ms | Generated tokens | Retrieval / truncation |
|---:|---:|---:|---:|---:|---:|---|
| 4,096 | 4,066 | 13,072.2 | 6,498.1 | 19,587.5 | 40 | All facts; EOS; not truncated |
| 8,192 | 8,191 | 23,861.1 | 6,896.2 | 31,742.4 | 40 | All facts; EOS; not truncated |
| 12,288 | 12,283 | 38,932.8 | 7,286.3 | 48,150.0 | 40 | All facts; EOS; not truncated |
| 15,360 | 15,352 | 51,847.7 | 7,582.7 | 62,313.1 | 40 | All facts; EOS; not truncated |

The near-16K request reserved 15,448 cells (15,352 input plus 96 output), 936 below the configured limit. At concurrency 2, both prompts were 6,112 tokens; with both 64-token reserves, the shared budget was 12,352 of 16,384 cells. Both retrieved all facts and generated 40 tokens, with client latencies of 54,007.2 and 54,567.4 ms; the pair completed in 54,978.8 ms wall time. There were no request errors, context truncations, or pod restarts. Final health was `{"status":"ok"}`.

| Device | Peak sampled VRAM | Device total | Headroom at sample peak |
|---|---:|---:|---:|
| GPU0, `f5b42f9b-16dd-cfe9-aab5-9a6ca129c29d` | 19,143 MiB | 23,040 MiB | 3,897 MiB (3.81 GiB) |
| GPU1, `5cc04de7-1ef7-af52-1e2f-ca71fa8200e3` | 18,725 MiB | 23,040 MiB | 4,315 MiB (4.21 GiB) |

VRAM is device-wide and sampled at a nominal 500 ms interval (816 samples in the stress artifact); short peaks may be missed and other allocations can affect the readings. These observations apply to this pinned model, image, driver, and node only; they do not prove a universal context/concurrency limit or imply preserved quality at arbitrary prompt lengths.

Reproduce the Q8 stress run against the reachable Service ClusterIP with:

```sh
python3 inference-platform/inference/llama-cpp-smoke/evaluate_32b.py \
	--mode long --profile q8-two-gpu-16k --timeout 300 \
	--output /tmp/q8-long-context.json
```

This performs one serial request at each target and one two-request concurrency run, captures per-request response and relevant logs, samples `nvidia-smi`, and records health, slots, and pod restart state. It stops on request failure, truncation, context-budget failure, relevant CUDA/OOM logs, unhealthy state, or restart. The run was followed by `kubectl apply -k inference-platform/kubernetes`; the final GPU Deployment is Q8_0 at 16,384 with one ready pod and two GPUs, the CPU Deployment is 1/1, and `kubectl diff -k inference-platform/kubernetes` is empty. Temporary Recreate rollouts caused brief planned GPU-Service downtime; exact downtime was not measured. The CPU service remained available throughout.

Focused runner checks: `pytest -q inference-platform/inference/llama-cpp-smoke/test_evaluate_32b.py` and `python3 -m py_compile inference-platform/inference/llama-cpp-smoke/evaluate_32b.py inference-platform/inference/llama-cpp-smoke/test_evaluate_32b.py`.