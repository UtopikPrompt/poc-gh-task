# llama.cpp CPU serving

This component defines a separate, single-replica CPU-only llama.cpp server and
ClusterIP endpoint, `llama-cpp-server-cpu:8081`. The deployment requests 2 CPU
and 2 GiB memory, with limits of 4 CPU and 4 GiB. Generation, batch, and HTTP
thread pools are each capped at 2 to match its CPU request. These conservative lab bounds fit the
staged 469 MiB Qwen2.5-0.5B Q4_K_M smoke model at a 1024-token context while
avoiding BestEffort scheduling.

The node-specific infrastructure overlay supplies the staged model filename,
read-only model PVC, and `k8s-node01` placement. The component explicitly uses
`--device none` and `--n-gpu-layers 0`; startup and readiness use `/health`, and
there is no liveness probe that could restart the server while the model loads.