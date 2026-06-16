# Serverless Platform — Architecture & Design

A self-service **FaaS (Function as a Service)** and **CaaS (Container as a Service)**
platform that wraps the open-source **Knative** project on **OpenShift**, exposed through a
**Python / FastAPI** REST API.

> **Status:** Design document (no implementation yet). This document is the source of truth
> for the architecture and is intended to be detailed enough for engineers to implement the
> FastAPI application, the Helm chart, and the GitOps manifests against an **airgapped**
> OpenShift environment.

---

## Table of Contents

1. [Overview & Goals](#1-overview--goals)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Core Service Offerings](#3-core-service-offerings)
4. [Multi-Site (Active/Active HA) Design](#4-multi-site-activeactive-ha-design)
5. [Networking & Exposure](#5-networking--exposure)
6. [Authentication & Authorization](#6-authentication--authorization)
7. [Secrets Management](#7-secrets-management)
8. [Deployment & GitOps](#8-deployment--gitops)
9. [Airgapped Considerations](#9-airgapped-considerations)
10. [REST API Specification](#10-rest-api-specification)
11. [Proposed Repository Layout](#11-proposed-repository-layout)
12. [Sample Manifests](#12-sample-manifests)
13. [Open Questions / Future Work](#13-open-questions--future-work)

---

## Design Decisions (locked in)

| Topic | Decision |
|-------|----------|
| Deliverable | Architecture/design doc only (this document) |
| FaaS build | **Knative Functions** (`func` + Cloud Native Buildpacks), mirrored builder images for airgap |
| Cluster auth | **cert-manager `Certificate` CR** (shipped in Helm chart) → client TLS cert; **CN is a DNS name** `serverless-api.clients.{base_domain}` (ACME-issued); that name is the Kubernetes user, bound via RBAC |
| Topology | **Two separate OpenShift clusters** ("sites") that **trust the same CA**. The **API runs active/active in both clusters**; a DNS record fronts the active API. **Workloads run on the same two clusters** in a **separate namespace** from the API. |
| Site selection | **Deploy to both sites on every deploy.** Each workload's **Route host is identical in both clusters**; a DNS record forwards to the active serverless site (active/passive at the traffic layer, active/active at the deploy layer). |
| Tenancy | **Shared namespace, label-scoped**; SSO group → resource labels enforced by the API |
| API authn | **SSO (Red Hat Build of Keycloak) OIDC** in front of the API |
| API authz | Based on **SSO group membership** |
| Secrets | **External Secrets Operator** — this repo ships **`ExternalSecret` only**, referencing a **pre-existing `ClusterSecretStore`** that points at **HashiCorp Vault** (API stores no secrets) |
| Route domain | Single platform wildcard **`*.serverless.{base_domain}`**; host `{name}-{group}.serverless.{base_domain}` (offering tracked as a label, not in the host) |
| CI/CD | **Helm** (this repo) + **ArgoCD** `ApplicationSet` (lives in a **separate GitOps repo**) |
| Environment | **Airgapped** — all images/deps mirrored to an internal registry; ACME via an internal ACME endpoint |

---

## 1. Overview & Goals

### Problem statement

Customers need to deploy workloads without managing Kubernetes/OpenShift directly. They
want two consumption models:

- **FaaS** — "give us your source code, we build and run it." The client provides a Git
  repository URL, branch, an access token, and the source lives in that repo. Supported
  runtimes: **Python, Go, JavaScript**.
- **CaaS** — "give us your image, we run it." The client provides a container image
  reference plus registry credentials (username + token).

Both models must run on **Knative Serving** (scale-to-zero, request-driven autoscaling) on
**OpenShift**, be reachable from outside the cluster via an **OpenShift Route**, and be
governed by enterprise SSO. Everything runs in an **airgapped** datacenter across **two
OpenShift clusters** for high availability. The **API itself also runs active/active on
those same two clusters** (fronted by a DNS record pointing at the active site), and the
**customer workloads run on the same two clusters** in a **separate namespace** from the API.

### Goals

- A single FastAPI REST API that abstracts Knative/OpenShift away from the customer.
- One API call deploys the workload to **both clusters**; the API is itself HA across both.
- Each workload exposed at a **single, cluster-independent Route host**, with DNS forwarding
  to the active site.
- Strong authn (SSO OIDC) and group-based authz.
- No secrets stored by the API; all secrets sourced from Vault via ESO.
- GitOps-managed (Helm + ArgoCD), reproducible, airgap-compatible.

### Non-goals (this phase)

- Implementation code (delivered later).
- Cross-site traffic steering is handled **outside** the API by a **DNS record that forwards
  to the active serverless site** (the Route host is identical in both clusters). The API is
  not a GSLB.
- Billing/metering, quota enforcement, and a full observability stack (see §13).

### Glossary

| Term | Meaning |
|------|---------|
| **Knative Serving** | Knative component that runs request-driven, autoscaling (incl. scale-to-zero) workloads. |
| **KSVC** | A Knative `Service` custom resource (`serving.knative.dev/v1`). The top-level unit we create per workload. |
| **Revision** | An immutable snapshot of a KSVC; created on each spec change. |
| **Route (OpenShift)** | OpenShift `route.openshift.io/v1` object that exposes a service externally over HTTP(S). |
| **Site** | One of the two independent OpenShift clusters the platform deploys to. |
| **SSO** | Red Hat Build of Keycloak — the OIDC identity provider. |
| **ESO** | External Secrets Operator — syncs secrets from Vault into Kubernetes Secrets. |
| **Tenant / group** | An SSO (Keycloak) group; the unit of ownership and isolation. |
| **`func`** | Knative Functions CLI / library used to build source into an OCI image via buildpacks. |

---

## 2. High-Level Architecture

```mermaid
flowchart TB
    U["User / CI client"]
    DNSAPI["DNS: serverless-api.{base_domain}<br/>→ active API site"]
    DNSAPP["DNS: *.serverless.{base_domain}<br/>→ active workload site"]
    KC["SSO / Keycloak OIDC (internal)"]
    REG[("Internal Container Registry<br/>(mirrored, airgapped)")]
    V[("HashiCorp Vault (existing)")]
    GIT[("GitOps repo (separate)<br/>ArgoCD ApplicationSet")]

    subgraph ZA["Site A — OpenShift Cluster A"]
        APIA["FastAPI API (active/active)"]
        KNA["Knative Serving<br/>(workloads namespace)"]
        RTA["OpenShift Route<br/>{name}-{group}.serverless.{base_domain}"]
        ESOA["ESO ExternalSecret"]
        CMA["cert-manager (ACME)"]
        KNA --> RTA
    end

    subgraph ZB["Site B — OpenShift Cluster B"]
        APIB["FastAPI API (active/active)"]
        KNB["Knative Serving<br/>(workloads namespace)"]
        RTB["OpenShift Route<br/>{name}-{group}.serverless.{base_domain}"]
        ESOB["ESO ExternalSecret"]
        CMB["cert-manager (ACME)"]
        KNB --> RTB
    end

    U -->|OIDC login| KC
    U -->|Bearer JWT + request| DNSAPI
    DNSAPI --> APIA
    DNSAPI -. failover .-> APIB

    APIA -->|"validate JWT / JWKS"| KC
    APIA -->|"create KSVC + Route (mTLS client cert)"| KNA
    APIA -->|"create KSVC + Route (mTLS client cert)"| KNB
    APIA -->|"pull/push images"| REG
    KNA --> REG
    KNB --> REG

    V -. secrets .-> ESOA
    ESOA --> APIA
    V -. secrets .-> ESOB
    ESOB --> APIB
    CMA -. "client cert (CN serverless-api.clients.base_domain)" .-> APIA
    CMB -. "client cert" .-> APIB
    GIT -. "Helm sync" .-> APIA
    GIT -. "Helm sync" .-> APIB

    U -.->|"workload traffic"| DNSAPP
    DNSAPP --> RTA
    DNSAPP -. failover .-> RTB
```

**Reading the diagram:**

- The user authenticates against **SSO** and calls the API via the **`serverless-api`
  DNS record**, which points at the **active API instance** (the API runs active/active on
  both clusters).
- The serving API validates the token (JWKS from SSO), authorizes on the user's **groups**,
  then **applies the KSVC + Route to both clusters** using each cluster's **client TLS cert**
  (CN `serverless-api.clients.{base_domain}`) for authentication.
- Each workload gets the **same Route host in both clusters**; the
  **`*.serverless.{base_domain}`** DNS record forwards end-user traffic to the active site.
- Images come from the **internal mirrored registry** (airgap). The API's own secrets come
  from **Vault via an ESO `ExternalSecret`** (using a pre-existing `ClusterSecretStore`); its
  client certs come from **cert-manager (ACME)**; the API is deployed by **Helm**, synced by
  an **ArgoCD `ApplicationSet` that lives in a separate GitOps repo**.

---

## 3. Core Service Offerings

Both offerings converge on the same primitive: **create/update a Knative `Service` (KSVC)
in both sites**, then ensure an OpenShift Route exists. They differ only in how the runnable
**image** is produced.

```mermaid
flowchart LR
    subgraph FaaS
        G["Git repo<br/>(URL, branch, token)"] --> B["Build via func / buildpacks<br/>(mirrored builder image)"]
        B --> I1["OCI image pushed to<br/>internal registry"]
    end
    subgraph CaaS
        IMG["Image ref + registry creds"] --> PS["Create imagePullSecret"]
        PS --> I2["Existing image in registry"]
    end
    I1 --> KSVC["Knative Service (both sites)"]
    I2 --> KSVC
    KSVC --> RT["OpenShift Route (per site)"]
```

### 3.1 FaaS — Function as a Service

**Inputs (request body):**

| Field | Required | Notes |
|-------|----------|-------|
| `gitUrl` | yes | HTTPS Git repository URL (internal Git, airgapped). |
| `branch` | yes | Branch / ref to build. |
| `gitToken` | yes | Repo access token; used only to clone, **never persisted** (see §7). |
| `runtime` | yes | One of `python`, `go`, `javascript`. |
| `name` | yes | Logical workload name (DNS-1123). |
| `env`, `files`, `scaling` | no | Shared capabilities, see §3.3. |

**Build flow (Knative Functions / buildpacks):**

1. The API launches a **build** (in-cluster) using **Knative Functions** (`func`) with
   **Cloud Native Buildpacks**. The builder/run images are the **mirrored** versions hosted
   in the internal registry (see §9) — buildpack autodetection picks the right
   Python/Go/JS buildpack.
2. Source is cloned from `gitUrl@branch` using `gitToken`.
3. The resulting OCI image is pushed to the **internal container registry** under a
   deterministic tag, e.g. `registry.internal/<group>/<name>:<gitsha>`.
4. The API then creates/updates the **KSVC** referencing that image (§3.3), in **both
   sites**.

> The build runs once and the **same image digest** is deployed to both sites to guarantee
> bit-for-bit parity across the active/active pair.

```mermaid
sequenceDiagram
    autonumber
    participant U as Client
    participant API as FastAPI API
    participant Build as func / buildpacks (build job)
    participant Reg as Internal Registry
    participant ZA as Site A (Knative)
    participant ZB as Site B (Knative)

    U->>API: POST /api/v1/functions (git, runtime, ...)
    API->>API: AuthN (JWT) + AuthZ (group)
    API->>Build: build(gitUrl@branch, runtime)
    Build->>Reg: push image @digest
    Build-->>API: image digest
    par Deploy to both sites (same digest)
        API->>ZA: apply KSVC + ensure Route
        API->>ZB: apply KSVC + ensure Route
    end
    ZA-->>API: route URL A, status
    ZB-->>API: route URL B, status
    API-->>U: 201 Created { sites: [A,B], urls, status }
```

### 3.2 CaaS — Container as a Service

**Inputs (request body):**

| Field | Required | Notes |
|-------|----------|-------|
| `image` | yes | Fully-qualified image reference in the internal registry (airgap). |
| `registryUsername` | yes | Registry username. |
| `registryToken` | yes | Registry access token; used to create an `imagePullSecret`, **not persisted** by the API. |
| `name` | yes | Logical workload name (DNS-1123). |
| `env`, `files`, `scaling` | no | Shared capabilities, see §3.3. |

**Flow:**

1. The API creates a Kubernetes `kubernetes.io/dockerconfigjson` **imagePullSecret** from
   the supplied credentials in each site, **labeled** with the owning group (§6) and linked
   to the KSVC's service account.
2. The API creates/updates the **KSVC** referencing `image` in **both sites**.

### 3.3 Shared capabilities (FaaS and CaaS)

Applied identically to both offerings; modeled on the KSVC pod spec.

| Capability | How it maps to Knative |
|------------|------------------------|
| **Environment variables** | Each `env` entry is `name` + `value`. A plain entry is set inline on the container; an entry with **`secret: true`** has its value moved into an API-created Kubernetes **Secret** (`{workload}-env`) and the container reads it via a `secretKeyRef` (the value is never inline). The API does **not** expose `valueFrom` — users cannot reference arbitrary existing cluster Secrets/ConfigMaps. |
| **Files (config & secret mounts)** | Via the `files` field, a user **uploads inline file content** (`content`/`contentBase64`), its `mountPath`, and an optional `readOnly` flag (default true). The API aggregates all non-secret files into **one `{workload}-files` ConfigMap** and all secret files (`secret: true`) into **one `{workload}-files` Secret** — one ConfigMap and one Secret per workload, a key per file — and mounts each at its path via `subPath`. (No referencing of pre-existing cluster objects.) |
| **Scaling options** | Knative autoscaling annotations: `autoscaling.knative.dev/min-scale`, `max-scale`, `target` (concurrency), and `containerConcurrency`. Scale-to-zero is the default when `min-scale=0`. |

A canonical scaling sub-object in the API:

```json
{
  "scaling": {
    "minScale": 0,
    "maxScale": 10,
    "targetConcurrency": 100,
    "containerConcurrency": 0
  }
}
```

---

## 4. Multi-Site (Active/Active HA) Design

The platform deploys **every** workload to **both** OpenShift clusters (Site A and Site B)
on each create/update, and the **API itself runs active/active on both clusters**. Because
both clusters **trust the same CA** and the workload **Route host is identical in both**,
each site is a full, independent replica; a DNS record forwards end-user traffic to the
active site.

The **client certificate and CA bundle are global** (the same identity/CA is valid in every
cluster), so a site profile is just its endpoint and namespace. The `routeDomain`, client
cert directory, and CA bundle are shared config:

```yaml
routeDomain: serverless.{base_domain}     # shared; same host in both clusters
clientCertDir: /etc/serverless/client     # tls.crt/tls.key (cert-manager), global
caBundle:                                 # OpenShift-injected, global
  configMap: trusted-ca-bundle
  key: ca-bundle.crt
  mountPath: /etc/serverless/trusted-ca
sites:
  - name: site-a
    apiServer: https://api.site-a.internal:6443
    namespace: serverless-workloads        # separate from the API's namespace
  - name: site-b
    apiServer: https://api.site-b.internal:6443
    namespace: serverless-workloads
```

> The API always authenticates with the **client certificate** (no in-cluster/ServiceAccount
> path) — uniform whether it's talking to its local cluster or the peer over its external API
> endpoint. Because `sites` carries no secrets, it can be sourced from a ConfigMap.

### Fan-out & status aggregation

- The API holds **one Kubernetes client per site** (built from that site's client cert + the
  shared CA).
- On deploy, it applies the KSVC + Route to both sites **concurrently** (async / thread
  pool), then **aggregates** per-site results. The workload `url` is the **same host** in
  both sites; only the per-site readiness differs:

```json
{
  "name": "orders-api",
  "type": "container",
  "url": "https://orders-api-team.serverless.example.com",
  "sites": [
    { "site": "site-a", "status": "Ready", "revision": "orders-api-00001" },
    { "site": "site-b", "status": "Ready", "revision": "orders-api-00001" }
  ],
  "overallStatus": "Ready"
}
```

### Partial-failure semantics

| Scenario | Behavior |
|----------|----------|
| Both sites succeed | `overallStatus = Ready`, `201`/`200`. |
| One site fails | `overallStatus = Degraded`, `207 Multi-Status`; the per-site object carries the error. The succeeded site is **left running** (HA prefers availability), and DNS keeps serving from the healthy site. |
| Both sites fail | `overallStatus = Failed`, `502`; the API attempts best-effort cleanup of any partially-created resources. |

- Operations are **idempotent** (apply/patch by name+group label), so a client can safely
  retry to heal a degraded deployment.
- **Build once, deploy the same digest to both sites** (see §3.1) so the two sites are
  identical.

> Cross-site traffic steering is handled by the **`*.serverless.{base_domain}` DNS record
> forwarding to the active site** — not by the API.

---

## 5. Networking & Exposure

- This runs on **OpenShift Serverless** (the Operator-installed Knative). The Serverless
  Operator's ingress controller **automatically creates the OpenShift `Route`** for each
  Knative ingress — so the platform requirement "every workload is exposed via an OpenShift
  Route" is satisfied **by the operator**, not by the API hand-creating Routes.
- A bare KSVC would only get a Route under the **per-cluster** default domain (`apps.<cluster>`),
  which differs between sites. To get **one stable, cluster-independent host**, the API creates
  a **`DomainMapping`** for `{name}-{group}.serverless.{base_domain}` in **each** cluster; the
  operator then provisions the Route for that host. A **`*.serverless.{base_domain}` DNS
  record forwards to the active site**.
- **TLS:** the custom host is covered by a **wildcard cert for `*.serverless.{base_domain}`**
  (provided to the DomainMapping / ingress); the operator-created Route is `edge`-terminated.

#### Route host convention (recommendation)

Use a **single platform wildcard domain** and put the tenant in the subdomain — do **not**
split FaaS/CaaS into separate domains:

```
{name}-{group}.serverless.{base_domain}
e.g. orders-api-team.serverless.example.com
```

Rationale: the host must be **identical in both clusters** (DNS forwards to active), so it
must be a custom platform domain anyway; FaaS-vs-CaaS is a build-time detail the consumer
shouldn't see in the URL; and one wildcard domain means **one wildcard cert + one DNS zone**
to manage. The offering (`faas`/`caas`) is tracked as a **label**, not in the host. The
`{group}` prefix prevents collisions in the shared namespace and makes ownership obvious.

**Object naming.** The OpenShift name of the workload (KSVC) and all its derived resources
(`{workload}-env` Secret, `{workload}-files` ConfigMap/Secret, pull secret) is
**`{name}-{group}`** — unique per tenant in the shared namespace.

**Custom hostname.** A client may override the host with a `hostname` field. Because the
`DomainMapping` name *is* the host, the API **validates the hostname is not already assigned**
to another workload before deploying (checked across both sites); a clash returns **409
Conflict**. The chosen host is recorded on the KSVC via the `serverless.platform/host`
annotation so reads can report the URL.

```mermaid
flowchart LR
    Ext["External client"] -->|HTTPS| DNS["DNS: *.serverless.{base_domain}<br/>→ active site"]
    DNS --> RT["OpenShift Route (operator-created from DomainMapping)<br/>{name}-{group}.serverless.{base_domain}"]
    RT --> KIN["Knative ingress (Kourier)"]
    KIN --> KSVC["KSVC revision pods"]
```

---

## 6. Authentication & Authorization

Two distinct identities are involved:

1. **End-user → API:** OIDC bearer token from **SSO**.
2. **API → each cluster:** **client TLS certificate** issued by **cert-manager** (ACME),
   whose **CN is the DNS name `serverless-api.clients.{base_domain}`**; that name is the
   Kubernetes user, bound by RBAC.

### 6.1 End-user authentication (SSO OIDC)

```mermaid
sequenceDiagram
    autonumber
    participant U as User / CI client
    participant KC as SSO (OIDC)
    participant API as FastAPI API

    U->>KC: Authenticate (OIDC / client credentials)
    KC-->>U: Access token (JWT) incl. groups claim
    U->>API: Request + Authorization: Bearer <JWT>
    API->>KC: Fetch JWKS (cached, internal URL)
    API->>API: Verify signature, issuer, audience, expiry
    API->>API: Extract groups claim -> authorize
    alt authorized
        API-->>U: 2xx (proceed)
    else not in required group
        API-->>U: 403 Forbidden
    end
```

- The API is a **resource server**: it validates JWTs offline using **JWKS** fetched from
  the internal SSO realm (cached, no per-request round trip).
- Validated: signature, `iss`, `aud`, `exp`/`nbf`.

#### Auth as an internal component (not a separate microservice)

All OIDC interaction is encapsulated in a **self-contained auth component inside the API**
(the `app/auth/` package — see §11), **not** a separately-deployed microservice. Because
token validation is **stateless** (verify signature against cached JWKS + read claims),
there is no shared state to centralize; a standalone auth service would only add a network
hop, another deployment to secure in both clusters, and a failure point. The component owns:

- SSO OIDC discovery + **JWKS fetch/cache** and **token validation** (`oidc.py`),
- **claims → group** mapping and admin/tenant policy (`claims.py`),
- the FastAPI **`require_auth` / `require_groups`** dependencies the routers use (`deps.py`).

> If auth-at-the-edge is ever wanted (to keep tokens out of app code / defense-in-depth), the
> OpenShift-native drop-ins are **oauth2-proxy** or **Authorino** as a sidecar/gateway — an
> infra change, not an API rewrite. (See §13.)

### 6.2 Group-based authorization (tenancy)

- Tenancy is **shared-namespace, label-scoped**. Every resource the API creates is labeled:

  ```yaml
  metadata:
    labels:
      serverless.platform/group: "<keycloak-group>"
      serverless.platform/managed-by: "serverless-api"
      serverless.platform/owner: "<sub or preferred_username>"
      # every resource created for a function/container also carries the workload name:
      serverless.platform/workload: "<function-or-container-name>"
  ```

  Every resource the API creates for a function/container (KSVC, Route,
  DomainMapping, the `{workload}-env` Secret, the `{workload}-files`
  ConfigMap/Secret, and the imagePullSecret) carries **both** the SSO group label
  and the workload-name label, so it is unambiguously attributable and selectable.

- The API derives the caller's group(s) from the **`groups` claim**. Authorization rules:
  - **Create/Update:** the workload is stamped with the caller's group label.
  - **Read/List:** results are filtered with a **label selector**
    `serverless.platform/group in (<caller groups>)`.
  - **Update/Delete by name:** the API first verifies the target resource's group label is
    in the caller's groups; otherwise `403`/`404`.
- A configurable mapping allows **admin groups** (full access) vs **tenant groups** (own
  resources only).

> Isolation is enforced **in the API layer** plus label selectors. Because all tenants share
> a namespace, the cluster RBAC for the API's service identity is namespace-wide (see §6.3);
> per-tenant isolation is therefore the API's responsibility. (A future hardening option is
> namespace-per-group — see §13.)

### 6.3 Cluster-side identity (cert-manager client cert + RBAC)

- The Helm chart ships a cert-manager **`Certificate`** per site, issued via **ACME** (an
  internal ACME endpoint in airgap). Because ACME requires the identity to be a DNS name, the
  cert's **CN/SAN is `serverless-api.clients.{base_domain}`** — and that DNS name is the
  **Kubernetes user**. OpenShift authenticates the client by that name. Both clusters
  **trust the same CA**, so the same identity is valid in either cluster.
- Each site has one `Role`/`RoleBinding` (in the **workload namespace**,
  `serverless-workloads`) granting least-privilege CRUD on exactly what the API manages:
  Knative `services`/`domainmappings`, `secrets`, `configmaps`, and read on `pods`/`events`.
  The API does **not** need `routes` permission — on OpenShift Serverless the operator
  creates the OpenShift Route automatically from the KSVC/DomainMapping.
- The cert is mounted **once** (global, not per-site) at `SERVERLESS_CLIENT_CERT_DIR`
  (`tls.crt`/`tls.key`); the API uses it to authenticate to **every** cluster via mTLS. There
  is no in-cluster/ServiceAccount fallback — always certificate-based.
- The CA used to verify the API servers is the **trusted CA bundle** (§9), pointed at by
  `SERVERLESS_CA_BUNDLE__*`; it is the same for every cluster.

---

## 7. Secrets Management

**Principle: the API never persists *its own platform* secrets**, and **ESO is used only for
those platform secrets** — never for customer workload data. There are three distinct
categories:

| Category | Owner / mechanism | ESO? |
|----------|-------------------|------|
| 7.1 **API's own platform secrets** (SSO client secret, client-cert material) | Vault → ESO `ExternalSecret` → K8s Secret | **Yes** |
| 7.2 **Customer credentials** (git/registry tokens) | Supplied per-request, used transiently | No |
| 7.3 **Customer config & secret mounts** (what the user wants inside their workload) | **Created and managed by the API directly**; readable back via the API | **No** |

### 7.1 The API's own platform secrets — Vault → ESO → Kubernetes Secret

The API needs, e.g., the SSO client secret and per-site client-cert material. These are
stored in **Vault** and projected into the cluster by **ESO**.

```mermaid
flowchart LR
    V[("HashiCorp Vault<br/>(existing)")]
    SS["ClusterSecretStore<br/>(pre-existing — NOT shipped by us)"]
    ES["ExternalSecret<br/>(shipped by this chart)"]
    K8S["Kubernetes Secret"]
    POD["FastAPI API pod"]

    V --> SS
    SS --> ES
    ES --> K8S
    K8S -->|"mounted / envFrom"| POD
```

- A **`ClusterSecretStore` already exists** in the clusters (it points at Vault via
  Kubernetes auth / AppRole). **This repo does NOT deploy a SecretStore/ClusterSecretStore.**
- This repo ships only **`ExternalSecret`** resources that **reference the existing
  `ClusterSecretStore`** and declare which Vault paths map to which Kubernetes Secret keys.
- ESO reconciles and keeps the Kubernetes Secret in sync; the API consumes it via `envFrom`
  or volume mounts. **No secret values live in Git or in the API's code/config.**

### 7.2 Customer-provided credentials (git/registry tokens)

- `gitToken` (FaaS) and `registryToken` (CaaS) arrive in the request body **over TLS**.
- They are used **transiently**:
  - `gitToken` is injected into the build job only for the clone and discarded.
  - `registryToken` becomes a labeled `imagePullSecret` attached to the workload's service
    account in each site.
- The API does **not** write these to its own datastore, logs, or Git. Where a credential
  must persist for the workload to run (the pull secret), it lives as a **scoped, labeled
  Kubernetes Secret** owned by the tenant group and is deleted when the workload is deleted.

### 7.3 Customer config & secret mounts (API-managed, **not ESO**)

When a user wants to mount config files or secret values **into their function/container**,
those are **created and managed by the API itself** — **not** through ESO/Vault.

```mermaid
flowchart LR
    U["User (FaaS/CaaS request)"]
    API["FastAPI API"]
    SEC["Kubernetes Secret / ConfigMap<br/>(labeled to group, both clusters)"]
    KSVC["Workload (KSVC) volumeMount / envFrom"]

    U -->|"create / update config or secret data"| API
    API -->|"create labeled Secret/ConfigMap"| SEC
    SEC --> KSVC
    U -->|"GET (read back)"| API
    API -->|"read by name, group-scoped"| SEC
```

- The API takes the user-supplied data and creates a Kubernetes **`Secret`** (for secret
  mounts) or **`ConfigMap`** (for config files) in the **workload namespace of both
  clusters**, stamped with the standard ownership labels (§6.2:
  `serverless.platform/group`, `managed-by`, `owner`).
- The workload references them via the `files` field (mounted at a `mountPath`) or `env`
  (`valueFrom`/`envFrom`). Inline uploads in `files` cause the API to create the backing
  resource automatically.
- **Read-back:** users can **retrieve their own config/secret resources through the API**
  (group-scoped — a caller only sees resources labeled with their group(s); see §10).
  Whether secret *values* are returned in clear or redacted by default is a configurable
  policy (see §13).
- **Lifecycle:** these resources are owned by the tenant group, kept consistent across both
  clusters by the API, and cleaned up when explicitly deleted or when the owning workload is
  deleted. **They never touch Vault or ESO.**

---

## 8. Deployment & GitOps

The FastAPI control-plane app is delivered via a **Helm chart that lives in this repo** and
is reconciled by an **ArgoCD `ApplicationSet` that lives in a separate, central GitOps repo**
(this repo does **not** contain the ArgoCD Application/ApplicationSet).

```mermaid
flowchart LR
    GITAPP[("GitOps repo (separate)<br/>ArgoCD ApplicationSet")]
    GITHELM[("This repo<br/>Helm chart + values")]
    ARGO["ArgoCD"]
    subgraph Cluster["OpenShift — each site (A and B)"]
        DEP["Deployment: serverless-api (active/active)"]
        CERT["cert-manager Certificate (ACME)"]
        RBAC["Role / RoleBinding (CN user)"]
        ESOC["ExternalSecret (refs existing ClusterSecretStore)"]
    end
    GITAPP --> ARGO
    ARGO -->|renders chart from| GITHELM
    ARGO --> DEP
    ARGO --> CERT
    ARGO --> RBAC
    ARGO --> ESOC
```

- **Helm chart (this repo)** templates: two `Namespace`s (`serverless-api` for the API and
  `serverless-workloads` for customer workloads, both annotated
  `argocd.argoproj.io/sync-options: Delete=false,Prune=false` so ArgoCD never prunes/deletes
  them), the trusted-CA-bundle `ConfigMap` (both namespaces), a `serverless-api-sites`
  **`ConfigMap`** holding just the OpenShift **sites data** (per-site API endpoint, name,
  namespace), loaded into the API as the `SERVERLESS_SITES` env var (the rest of the config is
  plain `env` on the Deployment), `Deployment`, `Service`, `Route` (for the API itself),
  `Role`/`RoleBinding` (bound to the client-cert CN user, in the workloads namespace),
  cert-manager `Certificate`, ESO **`ExternalSecret`** (referencing the pre-existing
  `ClusterSecretStore`), and `values.yaml` describing the site profiles. It does **not** ship a
  SecretStore, and the API pod runs as the namespace `default` ServiceAccount (cluster auth is
  the client certificate, not the SA token).
- **ArgoCD (separate GitOps repo)**: an `ApplicationSet` generates one Application **per
  site**, each pointing at this repo's chart with a per-site values file. Sync waves order
  Secrets/RBAC before the Deployment; health checks gate rollout.
- All referenced images are the **internal mirrored** images (airgap, §9).

### Platform prerequisites (installed separately)

This repo's chart **consumes** cluster capabilities that are installed and managed
**elsewhere** (a separate platform/cluster-bootstrap GitOps repo), not by this chart:

| Prerequisite | Provides | Install |
|--------------|----------|---------|
| **OpenShift Serverless Operator** | Knative Serving (`Service`/`DomainMapping` CRDs), kourier ingress in `knative-serving-ingress`, and **automatic OpenShift Route creation** for Knative ingresses | OLM `Subscription` → `KnativeServing` CR (mirrored for airgap via `oc-mirror`) |
| **cert-manager** | issues the API's ACME client certificate (§6.3) | OLM (mirrored) |
| **External Secrets Operator** + `ClusterSecretStore` | projects Vault secrets into the cluster (§7.1) | OLM (mirrored) |
| **RHBK** | OIDC identity provider (§6.1) | platform-managed |

On OpenShift you must use the **OpenShift Serverless Operator** — not an upstream/community
or Helm-based Knative install. The chart assumes the operator's conventions (kourier in
`knative-serving-ingress`, operator-managed Routes, the Knative CRDs).

---

## 9. Airgapped Considerations

Nothing may reach the public internet. Everything is mirrored to internal infrastructure.

| Concern | Approach |
|---------|----------|
| **Platform & app images** | Mirror to the internal registry; use `ImageDigestMirrorSet` / `ImageContentSourcePolicy` so image pulls resolve internally. |
| **Buildpack builder/run images** | Mirror the Cloud Native Buildpacks **builder** and **run** images used by Knative Functions for Python/Go/JS into the internal registry; configure `func` to use them. This is the key airgap dependency for FaaS. |
| **Python dependencies (the API)** | Build the API container against an **internal PyPI mirror** (e.g. Nexus/Artifactory) or vendored wheels; pin all versions. |
| **Function dependencies (per runtime)** | Buildpacks must resolve language deps from internal mirrors (internal PyPI, Go module proxy/`GOPROXY`, npm registry mirror). Documented as a prerequisite for each runtime. |
| **Base images** | Use mirrored UBI base images. |
| **CA trust** | A ConfigMap labelled `config.openshift.io/inject-trusted-cabundle: "true"` is created in **both** namespaces; OpenShift auto-populates it with the cluster's trusted CAs. It is **mounted into the API and every FaaS/CaaS workload** (and exported via `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`) so all internal TLS (Git, registry, Vault, SSO, the cluster API) is trusted. Same bundle for every cluster. |
| **cert-manager** | Issue client certs via **ACME against an internal ACME endpoint** (e.g. step-ca / internal CA exposing ACME) — not a public CA. Both clusters trust this CA, and the cert CN/SAN is the DNS name `serverless-api.clients.{base_domain}`. |
| **Helm charts** | Hosted in an internal chart repo / Git; no public chart pulls. |

---

## 10. REST API Specification

Base path: `/api/v1`. All endpoints require a valid SSO bearer token (§6). All responses
are JSON. Times are RFC 3339 UTC.

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/functions` | Create a FaaS workload (build from Git). **202 Accepted** — deploys in the background; poll `statusUrl`. |
| `GET` | `/api/v1/functions` | List caller's functions (label-scoped). |
| `GET` | `/api/v1/functions/{name}` | Get one function (spec + per-site status). |
| `PUT` | `/api/v1/functions/{name}` | Replace the function's mutable spec (env/files/scaling/hostname). **202 Accepted**. |
| `DELETE` | `/api/v1/functions/{name}` | Delete the function in both sites. |
| `POST` | `/api/v1/containers` | Create a CaaS workload. **202 Accepted** — deploys in the background; poll `statusUrl`. |
| `GET` | `/api/v1/containers` | List caller's containers (label-scoped). |
| `GET` | `/api/v1/containers/{name}` | Get one container (spec + per-site status). |
| `PUT` | `/api/v1/containers/{name}` | Replace the container's mutable spec (image/env/files/scaling/hostname). **202 Accepted**. |
| `DELETE` | `/api/v1/containers/{name}` | Delete the container in both sites. |
| `GET` | `/api/v1/{type}/{name}/status` | Per-site readiness, URLs, revision info. |
| `GET` | `/api/v1/{type}/{name}/logs` | (Optional) recent logs per site. |
| `POST` | `/api/v1/secrets` | Create a workload **secret** (API-managed, not ESO); applied to both clusters. |
| `GET` | `/api/v1/secrets` | List caller's secrets (label-scoped; values redacted). |
| `GET` | `/api/v1/secrets/{name}` | **Read back** one secret (values per policy — redacted by default). |
| `PUT` | `/api/v1/secrets/{name}` | Update a secret's data in both clusters. |
| `DELETE` | `/api/v1/secrets/{name}` | Delete the secret in both clusters. |
| `POST` | `/api/v1/configs` | Create a workload **config file / ConfigMap**; applied to both clusters. |
| `GET` | `/api/v1/configs` | List caller's configs (label-scoped). |
| `GET` | `/api/v1/configs/{name}` | **Read back** one config (full data). |
| `PUT` | `/api/v1/configs/{name}` | Update a config in both clusters. |
| `DELETE` | `/api/v1/configs/{name}` | Delete the config in both clusters. |
| `GET` | `/healthz`, `/readyz` | Liveness/readiness (no auth). |

> `secrets` and `configs` are **created and owned by the API** (§7.3), referenced from a
> workload's `files`/`env`, and are **readable back by their owning group** — they do
> **not** flow through ESO/Vault.

> **Async (submit + poll).** `POST`/`PUT` validate synchronously (so the caller gets
> immediate `400`/`404`/`409`), then **return `202 Accepted`** with `overallStatus: "Pending"`
> and a `statusUrl`; the build/deploy runs in the background. Clients poll
> `GET {statusUrl}` (`/api/v1/{type}/{name}/status`) until `overallStatus` is `Ready` (or
> `Degraded`). This suits slow FaaS builds and ServiceNow workflow patterns (§10.x).
>
> **Create is strict.** `POST /functions` and `POST /containers` **fail with 409** if a
> workload named `{name}-{group}` already exists in any site (it is not a silent upsert);
> changes go through the `PUT` endpoints.
>
> **`PUT` is a full replace** of the mutable spec (env/files/scaling/hostname; image for
> containers — defaults to the current image if omitted) and **404s** if the workload
> doesn't exist. Function code changes are not done via `PUT` (no git inputs); recreate.
>
> **Typed endpoints are offering-scoped:** `/functions/{name}` only acts on a function and
> `/containers/{name}` only on a container — a name that is the other offering returns 404.
> (The OpenShift object name stays `{name}-{group}`; the offering is a label, not in the name.)

### Shared sub-schemas

```jsonc
// Workload shared fields (used by both functions and containers)
{
  "name": "orders-api",                 // DNS-1123, required. OpenShift object name is {name}-{group}.
  "hostname": "orders.example.com",     // optional custom host; default {name}-{group}.{route_domain}.
                                        // must be a valid FQDN and not already assigned (else 409).
  "env": [                              // optional; each entry is name + value
    { "name": "LOG_LEVEL", "value": "info" },                       // inline
    { "name": "DB_PASSWORD", "value": "s3cret", "secret": true }    // -> API-created Secret {workload}-env
  ],
  "files": [                            // optional: inline files to mount
    // non-secret files -> one {workload}-files ConfigMap; secret files -> one {workload}-files Secret
    { "mountPath": "/etc/app/app.yaml", "content": "log_level: info\n", "secret": false, "readOnly": true },
    { "mountPath": "/etc/tls/tls.key",  "contentBase64": "<base64>",    "secret": true }
  ],
  "scaling": {                          // optional, see 3.3
    "minScale": 0, "maxScale": 10,
    "targetConcurrency": 100, "containerConcurrency": 0
  },
  "sites": ["site-a", "site-b"]         // optional; default = all sites (HA)
}
```

### FaaS — `POST /api/v1/functions`

Request:

```json
{
  "name": "image-resizer",
  "gitUrl": "https://git.internal/team/image-resizer.git",
  "branch": "main",
  "gitToken": "<repo-access-token>",
  "runtime": "python",
  "env": [ { "name": "MAX_PX", "value": "2048" } ],
  "scaling": { "minScale": 0, "maxScale": 20 }
}
```

Response `202 Accepted` (deploy runs in the background; poll `statusUrl`):

```json
{
  "name": "image-resizer",
  "type": "function",
  "runtime": "python",
  "url": "https://image-resizer-team.serverless.example.com",
  "overallStatus": "Pending",
  "sites": [],
  "statusUrl": "/api/v1/functions/image-resizer/status"
}
```

Then `GET /api/v1/functions/image-resizer/status` once Ready:

```json
{
  "name": "image-resizer",
  "type": "function",
  "url": "https://image-resizer-team.serverless.example.com",
  "overallStatus": "Ready",
  "sites": [
    { "site": "site-a", "status": "Ready", "revision": "image-resizer-00001" },
    { "site": "site-b", "status": "Ready", "revision": "image-resizer-00001" }
  ]
}
```

### CaaS — `POST /api/v1/containers`

Request:

```json
{
  "name": "orders-api",
  "image": "registry.internal/team/orders-api:1.4.2",
  "registryUsername": "svc-team",
  "registryToken": "<registry-token>",
  "env": [ { "name": "LOG_LEVEL", "value": "info" } ],
  "files": [ { "mountPath": "/etc/app/app.yaml", "content": "log_level: info\n", "secret": false } ],
  "scaling": { "minScale": 1, "maxScale": 8, "targetConcurrency": 50 }
}
```

Response `202 Accepted`: same envelope as the FaaS response (`type: "container"`, no
`runtime` build fields; `image` echoed back), then poll `statusUrl`.

### 10.x ServiceNow integration (frontend)

The API is the backend for a **ServiceNow** frontend; the design accommodates that:

- **Authentication — forward the end-user token.** ServiceNow obtains the user's **RHBK
  (OIDC) access token** (OAuth authorization-code / on-behalf-of) and sends it as the
  `Authorization: Bearer` header. The JWT carries the real user and `groups`, so the API's
  group-based authz (§6.2) works unchanged — actions are attributed to the actual requester.
  Configure ServiceNow as an OAuth client of RHBK whose tokens carry `aud = serverless-api`.
- **CORS.** When a ServiceNow Service Portal widget calls the API **from the browser**, set
  `SERVERLESS_CORS_ALLOW_ORIGINS` (Helm `corsAllowOrigins`) to the ServiceNow instance
  origin(s); the API enables CORS (preflight + `Authorization` header) only then. Server-side
  ServiceNow calls (IntegrationHub / Scripted REST) need no CORS.
- **Async submit + poll.** `POST`/`PUT` return **202** immediately with a `statusUrl`;
  the ServiceNow workflow polls `GET {statusUrl}` until `Ready`/`Degraded`. This avoids
  ServiceNow REST timeouts on slow FaaS builds and matches its long-running-task patterns.

### Workload secrets & configs (API-managed — `POST /api/v1/secrets`, `/api/v1/configs`)

The API creates these directly (§7.3) and the user can read them back. They are then
referenced from a workload's `files`/`env`.

```json
// POST /api/v1/secrets
{
  "name": "orders-tls",
  "data": { "tls.crt": "<base64>", "tls.key": "<base64>" }
}
```

```json
// POST /api/v1/configs
{
  "name": "orders-config",
  "data": { "app.yaml": "log_level: info\nfeature_x: true\n" }
}
```

Response `201 Created` (per-site applied; secret values redacted in responses by default):

```json
{
  "name": "orders-tls",
  "type": "secret",
  "keys": ["tls.crt", "tls.key"],
  "sites": [
    { "site": "site-a", "status": "Applied" },
    { "site": "site-b", "status": "Applied" }
  ],
  "overallStatus": "Applied"
}
```

### Error model

Standard envelope for all non-2xx responses:

```json
{
  "error": {
    "code": "SITE_PARTIAL_FAILURE",
    "message": "Deployment succeeded in site-a but failed in site-b.",
    "details": [
      { "site": "site-b", "reason": "ImagePullBackOff", "message": "registry auth failed" }
    ],
    "requestId": "b1c2..."
  }
}
```

| HTTP | Code | When |
|------|------|------|
| `400` | `VALIDATION_ERROR` | Bad/missing fields, unsupported runtime. |
| `401` | `UNAUTHENTICATED` | Missing/invalid JWT. |
| `403` | `FORBIDDEN` | Caller not in a required/owning group. |
| `404` | `NOT_FOUND` | Workload not found in caller's group scope. |
| `409` | `CONFLICT` | Name already exists for the group, or the requested `hostname` is already assigned. |
| `207` | `SITE_PARTIAL_FAILURE` | One site failed (Degraded). |
| `502` | `SITE_TOTAL_FAILURE` | Both sites failed. |
| `500` | `INTERNAL` | Unexpected error. |

---

## 11. Proposed Repository Layout

```text
Serverless/
├── README.md
├── docs/
│   └── ARCHITECTURE.md              # this document
├── app/                             # FastAPI application
│   ├── main.py                      # app factory, router registration, middleware
│   ├── core/
│   │   ├── config.py                # settings (site profiles, SSO, registry) via env/Secret
│   │   └── logging.py
│   ├── auth/                        # self-contained auth component (all OIDC interaction)
│   │   ├── oidc.py                  # SSO discovery + JWKS fetch/cache, token validation
│   │   ├── claims.py               # claims → group mapping, admin/tenant policy
│   │   └── deps.py                  # FastAPI dependencies: require_auth / require_groups
│   ├── routers/
│   │   ├── functions.py             # FaaS endpoints
│   │   ├── containers.py            # CaaS endpoints
│   │   ├── resources.py             # API-managed workload secrets & configs (§7.3)
│   │   └── health.py
│   ├── models/                      # Pydantic request/response schemas
│   │   ├── common.py                # env, scaling, files, site status
│   │   ├── function.py
│   │   ├── container.py
│   │   └── resource.py              # secret/config create/read-back schemas
│   ├── services/                    # business logic
│   │   ├── deployer.py              # multi-site fan-out + status aggregation
│   │   ├── builder.py               # FaaS build via func/buildpacks
│   │   ├── ksvc.py                  # KSVC manifest construction
│   │   ├── route.py                 # OpenShift Route construction
│   │   ├── resources.py             # CRUD + read-back of API-managed Secret/ConfigMap (§7.3)
│   │   └── secrets.py               # imagePullSecret / transient credential handling
│   └── clients/
│       ├── cluster.py               # Cluster: wraps the k8s library for one site (mTLS cert)
│       └── registry.py
├── helm/
│   └── serverless-api/
│       ├── Chart.yaml
│       ├── values.yaml              # site profiles, image refs, SSO, registry
│       └── templates/
│           ├── namespaces.yaml      # serverless-api + serverless-workloads (ArgoCD Delete=false,Prune=false)
│           ├── ca-bundle.yaml       # inject-trusted-cabundle ConfigMap in both namespaces
│           ├── configmap.yaml       # sites data (SERVERLESS_SITES) -> loaded as an env var
│           ├── deployment.yaml
│           ├── service.yaml
│           ├── route.yaml
│           ├── rbac.yaml            # Role/RoleBinding for the CN user (per site)
│           ├── certificate.yaml     # cert-manager Certificate (ACME, per site)
│           └── externalsecret.yaml  # ESO ExternalSecret (refs pre-existing ClusterSecretStore)
│   # NOTE: no secretstore.yaml — the ClusterSecretStore already exists in the clusters.
│   # NOTE: the ArgoCD ApplicationSet lives in a SEPARATE central GitOps repo, not here.
├── manifests/                       # standalone reference manifests / examples
│   └── examples/
├── tests/
│   ├── unit/
│   └── integration/
├── Containerfile                    # build the API image (airgap-friendly base)
├── pyproject.toml                   # pinned deps (internal PyPI mirror)
└── .helmignore / .dockerignore
```

---

## 12. Sample Manifests

> Illustrative only — final values are templated by Helm and parameterized per site.

### 12.1 Knative Service (KSVC)

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: orders-api-team        # {name}-{group}
  namespace: serverless-workloads
  labels:
    serverless.platform/group: team
    serverless.platform/workload: orders-api-team
    serverless.platform/managed-by: serverless-api
  annotations:
    serverless.platform/host: orders-api-team.serverless.example.com
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/min-scale: "1"
        autoscaling.knative.dev/max-scale: "8"
        autoscaling.knative.dev/target: "50"
    spec:
      containerConcurrency: 0
      imagePullSecrets:
        - name: orders-api-pull
      containers:
        - image: registry.internal/team/orders-api:1.4.2
          env:
            - name: LOG_LEVEL
              value: info
          volumeMounts:
            - name: app-config
              mountPath: /etc/app
            - name: trusted-ca           # injected CA bundle, mounted into every workload
              mountPath: /etc/serverless/trusted-ca
              readOnly: true
      volumes:
        - name: app-config
          configMap:
            name: orders-config
        - name: trusted-ca
          configMap:
            name: trusted-ca-bundle
```

### 12.1a Trusted CA bundle ConfigMap (both namespaces, OpenShift-injected)

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: trusted-ca-bundle
  namespace: serverless-workloads   # also created in serverless-api
  labels:
    config.openshift.io/inject-trusted-cabundle: "true"   # OpenShift fills .data
  annotations:
    argocd.argoproj.io/sync-options: Prune=false
# .data (ca-bundle.crt) is populated by OpenShift; configure ArgoCD to ignore it.
```

### 12.2 Knative DomainMapping (custom host; operator creates the Route)

> On OpenShift Serverless the API does **not** create an OpenShift Route. It creates a
> `DomainMapping` for the custom host in each cluster, and the Serverless Operator
> auto-provisions the corresponding Route. The host is identical in both clusters;
> `*.serverless.{base_domain}` DNS forwards to the active site.

```yaml
apiVersion: serving.knative.dev/v1beta1
kind: DomainMapping
metadata:
  name: orders-api-team.serverless.example.com   # the custom host
  namespace: serverless-workloads
  labels:
    serverless.platform/group: team
    serverless.platform/workload: orders-api-team
    serverless.platform/offering: caas
spec:
  ref:
    name: orders-api-team        # the {name}-{group} KSVC
    kind: Service
    apiVersion: serving.knative.dev/v1
```

### 12.3 cert-manager Certificate (cluster client cert, CN = DNS name, ACME)

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: serverless-api-site-a-client
  namespace: serverless-api
spec:
  secretName: site-a-client                       # mounted into the API pod
  commonName: serverless-api.clients.example.com  # DNS name => Kubernetes username
  dnsNames:
    - serverless-api.clients.example.com          # required for ACME issuance
  usages:
    - client auth
  issuerRef:
    name: internal-acme                # ACME ClusterIssuer (internal ACME endpoint, airgap)
    kind: ClusterIssuer
```

### 12.4 RBAC for the CN user (per site, shared workload namespace)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: serverless-api-workloads
  namespace: serverless-workloads
rules:
  - apiGroups: ["serving.knative.dev"]
    resources: ["services", "domainmappings"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["secrets", "configmaps"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
  - apiGroups: [""]
    resources: ["pods", "events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: serverless-api-workloads
  namespace: serverless-workloads
subjects:
  - kind: User
    name: serverless-api.clients.example.com   # matches the Certificate CN (DNS name)
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: serverless-api-workloads
  apiGroup: rbac.authorization.k8s.io
```

### 12.5 ESO — ExternalSecret only (references pre-existing ClusterSecretStore)

> The `ClusterSecretStore` already exists in the clusters and is **not** shipped by this
> repo. We deploy only the `ExternalSecret` below, referencing it by name.

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: serverless-api-secrets
  namespace: serverless-api
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend            # <-- name of the PRE-EXISTING ClusterSecretStore
    kind: ClusterSecretStore
  target:
    name: serverless-api-secrets   # consumed by the API via envFrom
  data:
    - secretKey: sso-client-secret
      remoteRef:
        key: serverless/api
        property: sso_client_secret
```

### 12.6 ArgoCD ApplicationSet — *reference only (lives in the separate GitOps repo)*

> This manifest is **not** part of this repository. It is shown so the platform team can wire
> this chart into the central GitOps repo's `ApplicationSet`, generating one Application per
> site that renders `helm/serverless-api` with a per-site values file.

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata:
  name: serverless-api
  namespace: argocd
spec:
  generators:
    - list:
        elements:
          - site: site-a
            cluster: https://api.site-a.internal:6443
            valuesFile: values-site-a.yaml
          - site: site-b
            cluster: https://api.site-b.internal:6443
            valuesFile: values-site-b.yaml
  template:
    metadata:
      name: "serverless-api-{{site}}"
    spec:
      project: serverless
      source:
        repoURL: https://git.internal/team/serverless.git   # THIS repo (the chart)
        targetRevision: main
        path: helm/serverless-api
        helm:
          valueFiles:
            - "{{valuesFile}}"
      destination:
        server: "{{cluster}}"        # deploy the API into each cluster (active/active)
        namespace: serverless-api
      syncPolicy:
        automated: { prune: true, selfHeal: true }
        syncOptions: [ "CreateNamespace=false" ]
```

---

## 13. Open Questions / Future Work

| Item | Notes |
|------|-------|
| **DNS failover automation** | Cross-site steering is the `*.serverless.{base_domain}` (and `serverless-api.{base_domain}`) DNS record forwarding to the active site. How the record's active target is flipped on a site outage (health checks, automation, TTLs) is owned by the networking team and out of scope here. |
| **Peer-cluster reachability** | The API talks to its peer cluster over that cluster's external API endpoint; confirm latency/firewall between sites and behavior when the peer is unreachable (the Degraded path covers this). |
| **Secret read-back policy** | For API-managed workload secrets (§7.3), decide the default on `GET /api/v1/secrets/{name}`: redact values, return masked, or return clear to the owning group — plus whether reads are audited. |
| **Quotas & rate limiting** | Per-group resource quotas (CPU/mem, max workloads) and API rate limiting are not yet specified. |
| **Observability** | Centralized logging/metrics/tracing for tenant workloads (and the `/logs` endpoint backing store) to be designed. |
| **Audit logging** | Who deployed/changed/deleted what — likely required for enterprise/compliance. |
| **Stronger isolation** | Optional move from shared-namespace to **namespace-per-group** for hard multi-tenancy. |
| **Build pipeline hardening** | Where `func` builds run (Tekton task vs. in-API job), build caching, and signed images (cosign in airgap). |
| **Rollback / versioning** | Knative revisions enable traffic splitting/rollback; expose this via the API later. |
| **Secret rotation** | cert-manager cert renewal + ESO refresh cadence and zero-downtime reload of the API clients. |
