#!/usr/bin/env bash
set -Eeuo pipefail

readonly LLAMA_IMAGE="ghcr.io/ggml-org/llama.cpp@sha256:c1d664830580e1651c1cc8d3bbc161af474800a56a83226e6e22f94bb8ba1c59"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'HELP'
Usage: MODEL_PATH=/absolute/path/to/model.gguf [MODEL_SHA256=<sha256>] [CONTAINER_CLI=docker] [GPU_INDEX=0] [GPU_LAYERS=8] [CTX_SIZE=1024] [MAX_TOKENS=48] [PORT=18080] bash run.sh

Requires an already-present local GGUF model, a Docker-compatible container CLI
with NVIDIA GPU support, a Tesla P40, and curl/nvidia-smi on the host. The
script does not download a model or build llama.cpp. The model directory is
mounted read-only into the pinned llama.cpp server image at /models.
HELP
    exit 0
fi

container_cli="${CONTAINER_CLI:-docker}"
model_path="${MODEL_PATH:-}"
expected_sha256="${MODEL_SHA256:-}"
gpu_index="${GPU_INDEX:-0}"
gpu_layers="${GPU_LAYERS:-8}"
ctx_size="${CTX_SIZE:-1024}"
max_tokens="${MAX_TOKENS:-48}"
port="${PORT:-18080}"

if [[ -z "$model_path" ]]; then
    printf 'MODEL_PATH is required and must point to an existing local GGUF file. See --help.\n' >&2
    exit 2
fi
[[ -f "$model_path" && -r "$model_path" ]] || {
    printf 'MODEL_PATH must be an existing readable file: %s\n' "$model_path" >&2
    exit 2
}
for tool in "$container_cli" curl nvidia-smi awk grep tail tr cut realpath mktemp; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'Required tool is missing: %s\n' "$tool" >&2
        exit 2
    }
done
model_path="$(realpath -- "$model_path")"
model_dir="$(dirname -- "$model_path")"
model_file="$(basename -- "$model_path")"
[[ "$model_file" == *.gguf ]] || {
    printf 'MODEL_PATH must name a .gguf file: %s\n' "$model_path" >&2
    exit 2
}

[[ "$gpu_index" =~ ^[0-9]+$ ]] || { printf 'GPU_INDEX must be a nonnegative integer.\n' >&2; exit 2; }
[[ "$gpu_layers" =~ ^[1-9][0-9]*$ ]] || { printf 'GPU_LAYERS must be a positive integer.\n' >&2; exit 2; }
[[ "$ctx_size" =~ ^[1-9][0-9]*$ ]] || { printf 'CTX_SIZE must be a positive integer.\n' >&2; exit 2; }
[[ "$max_tokens" =~ ^[1-9][0-9]*$ ]] && (( max_tokens <= 128 )) || {
    printf 'MAX_TOKENS must be an integer from 1 through 128.\n' >&2
    exit 2
}
[[ "$port" =~ ^[1-9][0-9]*$ ]] && (( port <= 65535 )) || {
    printf 'PORT must be an integer from 1 through 65535.\n' >&2
    exit 2
}
if [[ -n "$expected_sha256" ]]; then
    [[ "$expected_sha256" =~ ^[[:xdigit:]]{64}$ ]] || {
        printf 'MODEL_SHA256 must contain exactly 64 hexadecimal characters.\n' >&2
        exit 2
    }
    command -v sha256sum >/dev/null 2>&1 || {
        printf 'Required tool is missing: sha256sum (needed when MODEL_SHA256 is set).\n' >&2
        exit 2
    }
    actual_sha256="$(sha256sum -- "$model_path" | awk '{ print $1 }')"
    [[ "${actual_sha256,,}" == "${expected_sha256,,}" ]] || {
        printf 'Model SHA256 mismatch: expected %s, got %s\n' "$expected_sha256" "$actual_sha256" >&2
        exit 1
    }
    printf 'Model SHA256 verified: %s\n' "$actual_sha256"
fi

gpu_info="$(nvidia-smi --query-gpu=name,compute_cap,memory.total,memory.free --format=csv,noheader,nounits -i "$gpu_index")"
[[ "$gpu_info" == Tesla\ P40,* ]] || {
    printf 'Selected GPU is not a Tesla P40: %s\n' "$gpu_info" >&2
    exit 2
}
[[ "$(cut -d, -f2 <<<"$gpu_info" | tr -d ' ')" == 6.1 ]] || {
    printf 'Selected GPU does not report compute capability 6.1: %s\n' "$gpu_info" >&2
    exit 2
}

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/llama-cpp-smoke.XXXXXX")"
sampler_pid=""
server_pid=""
container_started=0
container_name="llama-cpp-smoke-$$"
cleanup() {
    if [[ -n "$sampler_pid" ]]; then
        kill "$sampler_pid" 2>/dev/null || true
        wait "$sampler_pid" 2>/dev/null || true
    fi
    if [[ -n "$server_pid" ]]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    if (( container_started )); then
        "$container_cli" rm -f "$container_name" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$work_dir"
    printf 'Temporary artifacts removed: %s\n' "$work_dir"
}
trap cleanup EXIT

printf 'Selected GPU %s: %s\n' "$gpu_index" "$gpu_info"
printf 'Using pinned llama.cpp image: %s\n' "$LLAMA_IMAGE"
printf 'Local model: %s (mounted read-only at /models/%s)\n' "$model_path" "$model_file"
printf 'Preconditions: Docker-compatible CLI with NVIDIA GPU support; no model download or source build will be performed.\n'

baseline_mib="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$gpu_index" | tr -d ' ')"
samples="$work_dir/gpu-memory-samples.txt"
server_log="$work_dir/server.log"
response_file="$work_dir/response.json"
printf 'Server command: %s run --rm --gpus %q -v %q:/models:ro %q -m %q --n-gpu-layers %q --ctx-size %q\n' \
    "$container_cli" "device=$gpu_index" "$model_dir" "$LLAMA_IMAGE" "/models/$model_file" "$gpu_layers" "$ctx_size"
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$gpu_index" --loop-ms=250 > "$samples" 2>/dev/null &
sampler_pid="$!"
"$container_cli" run --rm \
    --name "$container_name" \
    --gpus "device=$gpu_index" \
    -p "127.0.0.1:$port:8080" \
    -v "$model_dir:/models:ro" \
    "$LLAMA_IMAGE" \
    -m "/models/$model_file" \
    --n-gpu-layers "$gpu_layers" \
    --ctx-size "$ctx_size" \
    --host 0.0.0.0 \
    --port 8080 > "$server_log" 2>&1 &
server_pid="$!"
container_started=1

server_ready=0
for attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        server_ready=1
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
        break
    fi
    sleep 1
done
(( server_ready == 1 )) || {
    printf 'FAIL: llama-server did not become ready; server log follows.\n' >&2
    cat "$server_log" >&2
    exit 1
}

printf -v request_body '{"prompt":"Reply with exactly: llama.cpp GPU smoke test passed.","n_predict":%s,"temperature":0,"seed":1}' "$max_tokens"
printf 'Inference request: %s generated-token limit, context %s, GPU layers %s\n' "$max_tokens" "$ctx_size" "$gpu_layers"
curl --fail --silent --show-error --max-time 90 \
    -H 'Content-Type: application/json' \
    --data "$request_body" \
    "http://127.0.0.1:$port/completion" \
    --output "$response_file"
cat "$response_file"
printf '\n'

kill "$sampler_pid" 2>/dev/null || true
wait "$sampler_pid" 2>/dev/null || true
sampler_pid=""

kill "$server_pid" 2>/dev/null || true
wait "$server_pid" 2>/dev/null || true
server_pid=""
offload_line="$(grep -Eio 'offloaded [1-9][0-9]*/[0-9]+ layers to GPU|offloaded [1-9][0-9]* layers to GPU' "$server_log" | tail -n 1 || true)"
[[ -n "$offload_line" ]] || {
    printf 'FAIL: inference completed without evidence of nonzero GPU layer offload; server log follows.\n' >&2
    cat "$server_log" >&2
    exit 1
}
peak_mib="$(awk 'BEGIN { max = 0 } /^[0-9]+$/ { if ($1 > max) max = $1 } END { print max }' "$samples")"
(( peak_mib > 0 )) || {
    printf 'FAIL: GPU memory sampling produced no usable measurements.\n' >&2
    exit 1
}
printf 'GPU layer offload: %s\n' "$offload_line"
printf 'GPU memory used: baseline %s MiB, sampled peak %s MiB (250 ms sampling)\n' "$baseline_mib" "$peak_mib"
printf 'PASS: local model loaded, nonzero GPU offload verified, and bounded inference completed.\n'