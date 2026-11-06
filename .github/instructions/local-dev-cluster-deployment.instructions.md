---
applyTo: "codefactory/**, infra-codefactory/**"
---
# Local Dev Cluster Deployment

For Codefactory work that changes a service, the user's local K3s cluster is the development deployment target. This instruction is standing authorization to build and deploy the changed service to that local dev cluster as part of the task; it is not authorization to publish images remotely, deploy to staging or production, commit or push, or change unrelated workloads.

Before acting, inspect the working-tree changes and identify the owning service. Verify `kubectl config current-context` points to the intended local K3s cluster; do not switch contexts or assume a context is safe based only on its name. Use namespace `codefactory` explicitly, confirm the target Deployment exists there, and inspect its image, container name, and `imagePullPolicy` before changing anything. Stop if the context or target is uncertain.

Build only the changed service image from that service's Docker build context. Use the local image name/tag expected by its Deployment. This cluster uses local images with `imagePullPolicy: Never`: preserve and verify that policy on the target container so Kubernetes does not try to pull the image. Import only the built image into the K3s containerd image store; use `sudo` for Docker or containerd commands only when local permissions require it. Do not use `sudo` for `kubectl` or expose credentials in commands.

Roll out only the changed service's Deployment in `codefactory`, setting its container to the imported image or restarting that Deployment if the image tag is unchanged. Do not apply the full dev Kustomize overlay or run the multi-service deploy script for this one-service update: the checked-in overlay currently declares `imagePullPolicy: Always` and includes other workloads and the migration Job. Verify the target Deployment rollout completes, then verify the changed behavior through that service's endpoint, using a localhost-only port-forward when appropriate. Clean up any port-forward afterward.
