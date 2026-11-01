# Inference Platform

This project owns local inference experiments, serving manifests, node-specific
infrastructure overlays, and their operator notes. Its Kubernetes root is
`inference-platform/kubernetes`; the Kustomize tree is independent of the
Codefactory services.

Render the complete inference serving and storage configuration from the
repository root:

```sh
kubectl kustomize inference-platform/kubernetes
```

The llama.cpp smoke-test commands and measured artifacts are documented in
[`inference/llama-cpp-smoke/README.md`](inference/llama-cpp-smoke/README.md).