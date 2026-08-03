# Containers (CaaS)

Running a container image the caller already has. What containers share with
functions - scaling, env, files, hosts, status - is in ARCHITECTURE.md.

## Contents

- [Overview](#overview)
- [API - create & update](#api---create--update)

## Overview

**Inputs (request body):**

| Field | Required | Notes |
|-------|----------|-------|
| `image` | yes | Fully-qualified image reference in the internal registry (airgap). |
| `registryUsername` | no | Registry username. Optional - omit both creds for a public image; if either is given, **both** are required. Returned on GET (`spec.registryUsername`). |
| `registryToken` | no | Registry access token; used to create an `imagePullSecret`, **not persisted** and **never returned**. |
| `name` | yes | Logical workload name (DNS-1123). |
| `port` | no | Container port the workload listens on. Defaults to **8080** - what Knative injects as `$PORT`, and what most images serve on - and is stamped explicitly on the KSVC so a read reports it rather than leaving it to convention. Send it only when the image serves elsewhere: nothing can detect that, so a mismatch shows up as a revision that never becomes ready (the cause lands on the per-site `error`), not as a rejected request. Replaced on `PUT`, so omitting it returns the workload to 8080. Bounds and the default are advertised on `GET /api/v1/containers/info`. |
| `env`, `files`, `scaling` | no | Shared capabilities, see ARCHITECTURE.md: Shared capabilities. |

**Flow:**

1. The API creates a Kubernetes `kubernetes.io/dockerconfigjson` **imagePullSecret** from
   the supplied credentials in each site, **labeled** with the owning group (ARCHITECTURE.md: Authentication & Authorization) and linked
   to the KSVC's service account. The secret's `auths` entry is keyed to the **registry host
   parsed from the client's `image`** (the org runs several registries), not the platform's
   own registry.
2. The API creates/updates the **KSVC** referencing `image` in **both sites**.

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