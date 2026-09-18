#!/usr/bin/env bash
# Create (or reuse) the local kind cluster and make it reachable.
#
# docker compose runs this as the cluster-bootstrap service, with the Docker
# socket mounted so kind can start the node container on the host daemon. It
# also runs directly on a host that has docker, kind and kubectl.
#
# Idempotent: re-running reuses an existing cluster, restarts a stopped node,
# and rewrites the kubeconfigs.
set -euo pipefail

CLUSTER="${KIND_CLUSTER_NAME:-opsforge}"
CONFIG="${KIND_CONFIG:-$(dirname "$0")/kind-config.yaml}"
NODE="${CLUSTER}-control-plane"
# Compose network the backend lives on (empty: skip, e.g. when run on a host).
NETWORK="${COMPOSE_NETWORK:-}"
# Where to write the kubeconfig that uses the node's in-network address.
INTERNAL_KUBECONFIG="${INTERNAL_KUBECONFIG:-}"
# Where to write the kubeconfig for tools on the host (127.0.0.1:16443).
HOST_KUBECONFIG="${HOST_KUBECONFIG:-}"
PREPULL_IMAGE="${PREPULL_IMAGE:-python:3.12-alpine}"

log() { printf '[cluster-up] %s\n' "$*"; }

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "kind cluster '$CLUSTER' exists"
  if [ "$(docker inspect -f '{{.State.Running}}' "$NODE")" != "true" ]; then
    log "node container stopped; starting it"
    docker start "$NODE" >/dev/null
  fi
else
  log "creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --config "$CONFIG" --wait 180s
fi

# Put the node on the compose network so the backend reaches the API server
# (${NODE}:6443) and the NodePorts by container name.
if [ -n "$NETWORK" ]; then
  if docker inspect -f '{{json .NetworkSettings.Networks}}' "$NODE" | grep -q "\"$NETWORK\""; then
    log "node already on network $NETWORK"
  else
    docker network connect "$NETWORK" "$NODE"
    log "connected $NODE to $NETWORK"
  fi
fi

if [ -n "$INTERNAL_KUBECONFIG" ]; then
  mkdir -p "$(dirname "$INTERNAL_KUBECONFIG")"
  kind get kubeconfig --name "$CLUSTER" --internal > "$INTERNAL_KUBECONFIG"
  # The backend reads it as a non-root user.
  chmod 0644 "$INTERNAL_KUBECONFIG"
  log "wrote internal kubeconfig -> $INTERNAL_KUBECONFIG"
fi
if [ -n "$HOST_KUBECONFIG" ]; then
  mkdir -p "$(dirname "$HOST_KUBECONFIG")"
  kind get kubeconfig --name "$CLUSTER" > "$HOST_KUBECONFIG"
  log "wrote host kubeconfig -> $HOST_KUBECONFIG"
fi

# Wait for the API server through whichever kubeconfig we can use from here.
PROBE_KUBECONFIG="${INTERNAL_KUBECONFIG:-$HOST_KUBECONFIG}"
if [ -n "$PROBE_KUBECONFIG" ]; then
  for _ in $(seq 1 60); do
    if kubectl --kubeconfig "$PROBE_KUBECONFIG" get --raw /readyz >/dev/null 2>&1; then
      log "API server ready"
      break
    fi
    sleep 2
  done
fi

# Pull the workload image into the node once, so the first rollout does not
# wait on a registry. Failure is not fatal: the kubelet pulls on demand.
if docker exec "$NODE" crictl inspecti "$PREPULL_IMAGE" >/dev/null 2>&1; then
  log "image $PREPULL_IMAGE already present on node"
else
  log "pre-pulling $PREPULL_IMAGE into the node"
  docker exec "$NODE" crictl pull "$PREPULL_IMAGE" >/dev/null || log "pre-pull failed; kubelet will pull on demand"
fi

log "cluster ready"
