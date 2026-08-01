# Documentation

The code is the source of truth. These documents explain intent and the reasoning
behind a design; where one disagrees with the code, the code is right and the
document is a bug.

| Document | Subject |
|----------|---------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | The platform as a whole: goals, multi-site active/active, networking, auth, secrets, airgap, and the REST conventions both offerings share. |
| [CONTAINERS.md](./CONTAINERS.md) | The container offering (CaaS): running an image the caller already has. |
| [FUNCTIONS.md](./FUNCTIONS.md) | The function offering (FaaS): running code built from source, and how build state folds into a function's status. |
| [BUILDING.md](./BUILDING.md) | How source becomes an image: kpack, buildpacks, runtime versions, credentials, and the build flow. |
| [DEPLOYING.md](./DEPLOYING.md) | Installing and operating the platform: charts, GitOps, RBAC, sample manifests. |

Start with ARCHITECTURE.md. Read the offering document for the workload type you
care about, then BUILDING.md if it is a function.

Sections are referenced by title (`BUILDING.md: Build Flow`) rather than by
number. Numbered sections made every cross-reference break whenever a document
was reordered, which is what left the previous two files hard to navigate.
