# Containers (CaaS)

Running a container image the caller already has. What containers share with
functions - scaling, env, files, hosts, status - is in ARCHITECTURE.md.

## Overview

**Inputs (request body):**

| Field | Required | Notes |
|-------|----------|-------|
| `image` | yes | Fully-qualified image reference in the internal registry (airgap). |
| `registryUsername` | no | Registry username. Optional - omit both creds for a public image; if either is given, **both** are required. Returned on GET as the top-level `registryUsername`, like a secret's name. |
| `registryToken` | no | Registry access token; used to create an `imagePullSecret`, **not persisted** and **never returned**. |
| `name` | yes | Logical workload name (DNS-1123). |
| `port` | no | Container port the workload listens on. Defaults to **8080** - what Knative injects as `$PORT`, and what most images serve on - and is stamped explicitly on the KSVC so a read reports it rather than leaving it to convention. Send it only when the image serves elsewhere: nothing can detect that, so a mismatch shows up as a revision that never becomes ready (the cause lands on the per-region `message`), not as a rejected request. Replaced on `PUT`, so omitting it returns the workload to 8080. Bounds and the default are advertised on `GET /api/serverless/v1/containers/info`. |
| `env`, `files`, `scaling` | no | Shared capabilities, see API.md: Shared sub-schemas. |

**Flow:**

1. The API creates a Kubernetes `kubernetes.io/dockerconfigjson` **imagePullSecret** from
   the supplied credentials in each region, **labeled** with the owning group (API.md: Authentication & Authorization) and linked
   to the KSVC's service account. The secret's `auths` entry is keyed to the **registry host
   parsed from the client's `image`** (the org runs several registries), not the platform's
   own registry.
2. The API creates/updates the **KSVC** referencing `image` in **both regions**.

## API - create & update

Request:

```json
{
  "name": "orders-api",
  "image": "registry.internal/team/orders-api:1.4.2",
  "registryUsername": "svc-team",
  "registryToken": "<registry-token>",
  // registryUsername/registryToken are optional (omit both for a public image)
  "env": [ { "name": "LOG_LEVEL", "value": "info" } ],
  "files": [ { "mountPath": "/etc/app/app.yaml", "content": "log_level: info\n", "secret": false } ],
  "scaling": { "minScale": 1, "maxScale": 8, "metric": "concurrency", "target": 50 }
}
```

Response `202 Accepted`: same envelope as the FaaS response (`type: "container"`, no
`runtime` build fields; `image` echoed back), then poll `statusUrl`.

`PUT` is a **full replace**, so `image` is required on update exactly as on create, and an
omitted `port` returns the workload to 8080. Only redacted secret material is keep-on-omit
(FUNCTIONS.md: Editing a workload - the recipes there cover both offerings).

## Pulling the tag again

`POST /api/serverless/v1/groups/{group}/containers/{name}/pull`, no request body.

Knative resolves `image` to a digest **once**, when the revision is created, and pins the
pods to that digest. Re-pushing `orders-api:1.4.2` therefore changes nothing: the running
revision keeps the digest it resolved, and a `PUT` with the same image is a no-op that
produces no new revision. `imagePullPolicy: Always` does not help either - the Deployment
is already pinned to a digest, so it re-pulls the same bytes.

What does work is a **new revision**, which resolves the tag again. This endpoint writes
one annotation to make Knative cut one:

```
metadata.annotations["serverless.platform/pull-stamp"]                  the stored copy
spec.template.metadata.annotations["serverless.platform/pull-stamp"]    what Knative diffs
```

Both carry the same value, minted once per request and written to **every** region - a
per-region value would leave the regions on different revisions. It is a merge patch rather
than the platform's usual full apply, for the same reason the function rebuild trigger is
(BUILDING.md: What causes a new Build): nothing about the desired state changes, and the
workload was just read, so there is nothing to create.

The metadata copy is the load-bearing one. `build_ksvc` re-stamps it from the workload's
stored state on every apply, so the next ordinary `PUT` carries the stamp forward instead
of dropping it - dropping it is itself a template change, and would cut a second revision
nobody asked for.

**A digest-pinned container is a `400`.** `image` given as `...@sha256:...` names one
immutable object; a new revision would resolve the same digest. Send a `PUT` with a tag to
track one.

**Functions do not have this endpoint.** A function's image is built by the platform, and
its digest reaches the workload through the build controller
(BUILD-CONTROLLER.md: Digest propagation). `POST .../functions/{name}/build` is the equivalent:
build again, then let the controller roll the result out.
