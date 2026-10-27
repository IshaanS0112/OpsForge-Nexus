# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). Please do not open a public issue for
a security problem.

## What this project is

OpsForge Nexus is a reliability platform that runs on a local Kubernetes cluster
for development and demonstration. It is **not hardened for production or for
exposure to a network you do not control**, and a few parts are deliberately
permissive so the system can be driven easily:

| Area | State | If you deploy it anywhere real |
|---|---|---|
| API authentication | None — single implicit tenant | Put it behind an authenticating proxy |
| CORS | `*` | Restrict to the console's origin |
| Chaos endpoints | `POST /chaos/inject` kills pods and injects faults, unauthenticated | Require authorisation; never expose publicly |
| Cluster access | The backend holds a kubeconfig with write access to its namespace | Scope the service account to the namespace it owns |
| Demo workload | `/admin/fault` accepts runtime faults, with a TTL enforced in the pod | Remove the endpoint outside the demo fleet |

## Secrets

`LLM_API_KEY` is read from the environment and never logged or persisted.
RCA reports store the signals the model was given, not credentials. `.env` files
and the generated kubeconfig under `.kube/` are git-ignored; nothing in the
repository contains a key.

## Dependencies

Backend and frontend dependencies are pinned. CI runs on every push and pull
request to `main`.
