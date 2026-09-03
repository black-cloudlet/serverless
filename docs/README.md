# Documentation

The code is the source of truth. These documents explain intent and the reasoning
behind a design; where one disagrees with the code, the code is right and the
document is a bug.

**Start with [ARCHITECTURE.md](./ARCHITECTURE.md).** It is the overview: what the
platform is, the three services it runs, and a walkthrough of one create request
naming which service does what. Everything else is detail behind a step of that
walkthrough.

## The platform

| Document | Subject |
|----------|---------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | The platform as a whole: goals, the component map, how a request flows end to end, networking, secrets, and airgap. |
| [DEPLOYING.md](./DEPLOYING.md) | Installing and operating it: charts, GitOps, RBAC, sample manifests. |

## The services

| Document | Subject |
|----------|---------|
| [API.md](./API.md) | The API: the REST surface, how a request is handled, the fan-out to both regions and what a partial answer means, auth, and the error model. |
| [STREAMING.md](./STREAMING.md) | The streaming endpoints: what each emits, reading once instead of following, how a browser authenticates one, and what bounds them. |
| [TENANT-CONTROLLER.md](./TENANT-CONTROLLER.md) | The tenant controller: a namespace per group, the template set, provisioning, the reconcile loop, and namespace GC. |
| [BUILD-CONTROLLER.md](./BUILD-CONTROLLER.md) | The build controller: rolling a built digest onto the workload, and registry tag GC. |

## The offerings

| Document | Subject |
|----------|---------|
| [FUNCTIONS.md](./FUNCTIONS.md) | The function offering (FaaS): code built from source, and how build state folds into a function's status. |
| [CONTAINERS.md](./CONTAINERS.md) | The container offering (CaaS): an image the caller already has. |
| [BUILDING.md](./BUILDING.md) | How a function's source becomes an image: kpack, buildpacks, credentials, and the build flow. |
| [RUNTIMES.md](./RUNTIMES.md) | Reference: runtime versions, dependency mirroring, the registry layout, and the airgap mirror inventory. |

## Reading order

Read ARCHITECTURE.md, then the offering document for the workload type you care
about. From there follow the walkthrough into whichever service you need:
API.md for anything a client sees, BUILDING.md and RUNTIMES.md for a function's
image, and the two controller documents for what happens after a request
returns.

Sections are referenced by title (`BUILDING.md: Build Flow`) rather than by
number, so a cross-reference survives a document being reordered. Docstrings in
the code cite the same way, as `docs/BUILDING.md - Build Flow`.
