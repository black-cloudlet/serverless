# Serverless

A self-service **FaaS (Function as a Service)** and **CaaS (Container as a Service)**
platform that wraps the open-source [Knative](https://knative.dev/) project on **OpenShift**,
exposed through a **Python / FastAPI** REST API.

- **FaaS** - clients provide a Git repo (URL, branch, token) and source code; runtimes are
  **configurable** (the chart ships **Python, Go, Node**; listed on
  `GET /api/v1/functions/info`). Built in-cluster by **kpack** with Cloud Native Buildpacks.
- **CaaS** - clients provide a container image plus registry credentials.
- Both support **env vars**, **mounted secrets/config files**, and **scaling options**, and
  are exposed externally via **OpenShift Routes**.
- Auth via **SSO (Keycloak) OIDC** with **SSO group-based** authorization.
- Deployed via **Helm + ArgoCD**; secrets sourced from **HashiCorp Vault** through the
  **External Secrets Operator**.
- Designed for **two OpenShift clusters (active/active HA)** in an **airgapped** environment.

## Documentation

Start with **[docs/](docs/README.md)**, which indexes the set:

- **[Architecture & Design](docs/ARCHITECTURE.md)** - the platform as a whole: multi-site
  active/active, networking, auth, secrets, airgap, and the REST conventions both
  offerings share.
- **[Functions](docs/FUNCTIONS.md)** / **[Containers](docs/CONTAINERS.md)** - the two offerings.
- **[Building](docs/BUILDING.md)** - how source becomes an image (kpack + buildpacks).
- **[Deploying](docs/DEPLOYING.md)** - charts, GitOps, RBAC, sample manifests.
- **[Changelog](CHANGELOG.md)** - notable changes per release.

The code is the source of truth; where a document disagrees with it, the document is
the bug.

## Layout

```
api/        the control-plane API service (python -m api.main)
  auth/     self-contained OIDC auth component (SSO)
  models/   Pydantic request/response schemas
  services/ workload engine + offerings, split by responsibility:
            manifests/ (build what gets applied), sites/ (fan-out + per-site
            read/write), state/ (interpret what came back), builder/ (image build)
  routers/  functions / containers / info (public) endpoints
controller/ the build controller (python -m controller.main): watches kpack
            Images and rolls each finished build's digest onto the function's
            Knative Service in every site. Serves no HTTP; its own image, built
            from Dockerfile.controller with no web stack installed.
common/     shared by api + controller: build domain, cluster client (mTLS),
            settings, /healthz+/readyz + offline docs, labels, errors, logging
charts/     Helm chart (2 Deployments, Route, RBAC, Certificate, ExternalSecret, NetworkPolicy)
tests/      unit + API tests
dev/        sample runtimes file, for running the API locally
.github/    CI/CD workflows: checks (reusable), ci, release
Dockerfile            the API image
Dockerfile.controller the build controller image
```

The ArgoCD `ApplicationSet` is **not** in this repo - it lives in the central GitOps repo
and renders `charts/serverless-api` once per site (docs/DEPLOYING.md - Deployment & GitOps).

## Develop

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the test suite
pytest

# Run locally (auth disabled for dev; no cluster calls until you deploy a workload)
cp .env.example .env
SERVERLESS_AUTH_ENABLED=false uvicorn api.main:app --reload
# Interactive API docs at http://127.0.0.1:8000/docs
```

`.env.example` points `SERVERLESS_RUNTIMES_FILE` at `dev/runtimes.yaml`. That file is not
optional: the runtimes list has no built-in fallback, so the app refuses to start without
it (docs/BUILDING.md - Runtime Versions & Dependencies). The configured sites are not
reachable locally either; that is expected - the startup warmup logs a warning and the
API serves, since cluster connections are lazy.

Configuration is environment-driven (`SERVERLESS_*`); see `.env.example`. In production the
values are projected from Vault via the External Secrets Operator
(docs/ARCHITECTURE.md - Secrets Management).

> **Status:** Implemented end to end - endpoints, auth, multi-site deployer, manifest
> builders, kpack builds and the build controller that rolls each finished digest out -
> with unit/API tests. Not yet implemented: the per-function **git webhook** that would pin
> a pushed commit SHA to a build (`BuildRequest.revision` already carries the field); until
> then a build follows the branch head, and `POST .../functions/{name}/build` is the
> on-demand trigger.
