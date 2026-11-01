# llama.cpp GPU Smoke Pod

This temporary infrastructure-owned Pod checks the node-local model PVC and
one NVIDIA GPU. It is separate from the generic inference project and uses the
upstream llama.cpp image by digest. The container entrypoint is left unchanged;
the Pod supplies only server arguments. Verbose server logging is enabled to
make backend and GPU-offload evidence visible.

## Run

From the repository root, render and validate both bundles before applying:

```sh
kubectl kustomize inference-platform/kubernetes/infrastructure/overlays/k8s-node01/inference-storage
kubectl apply --dry-run=client --validate=strict -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/inference-storage
kubectl kustomize inference-platform/kubernetes/infrastructure/overlays/k8s-node01/llama-cpp-smoke
kubectl apply --dry-run=client --validate=strict -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/llama-cpp-smoke
```

Apply storage first, then the temporary Pod:

```sh
kubectl apply -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/inference-storage
kubectl apply -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/llama-cpp-smoke
kubectl -n inference wait --for=condition=Ready pod/llama-cpp-gpu-smoke --timeout=10m
kubectl -n inference logs llama-cpp-gpu-smoke
```

The logs must include nonzero GPU layer offload (for example,
`offloaded 24/24 layers to GPU`). Keep port-forwarding active while sending one
bounded request:

```sh
kubectl -n inference port-forward pod/llama-cpp-gpu-smoke 18080:8080
curl --fail-with-body --silent --show-error --max-time 60 \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Reply with one short greeting.","n_predict":24,"temperature":0}' \
  http://127.0.0.1:18080/completion
```

Require HTTP success, nonempty generated `content`, and the GPU offload log for
a passing smoke test. Stop port-forwarding with Ctrl-C. Delete only the
temporary Pod afterward, including after a failed test:

```sh
kubectl delete -k inference-platform/kubernetes/infrastructure/overlays/k8s-node01/llama-cpp-smoke --ignore-not-found
```

Leave the namespace, StorageClass, PV, PVC, and model in place. Do not delete
the inference-storage overlay as cleanup.