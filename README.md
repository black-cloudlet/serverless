# Serverless

A self-service **FaaS (Function as a Service)** and **CaaS (Container as a Service)**
platform that wraps the open-source [Knative](https://knative.dev/) project on **OpenShift**,
exposed through a **Python / FastAPI** REST API.

- **FaaS** — clients provide a Git repo (URL, branch, token) and source code; supported
  runtimes are **Python, Go, JavaScript**. Built with Knative Functions / buildpacks.
- **CaaS** — clients provide a container image plus registry credentials.
- Both support **env vars**, **mounted secrets/config files**, and **scaling options**, and
  are exposed externally via **OpenShift Routes**.
- Auth via **RHBK (Keycloak) OIDC** with **SSO group-based** authorization.
- Deployed via **Helm + ArgoCD**; secrets sourced from **HashiCorp Vault** through the
  **External Secrets Operator**.
- Designed for **two OpenShift clusters (active/active HA)** in an **airgapped** environment.

## Documentation

- **[Architecture & Design](docs/ARCHITECTURE.md)** — the full design document: component and
  sequence diagrams, REST API specification, repository layout, and sample manifests.

> **Status:** Design phase. No application code yet — see the architecture document for the
> intended implementation.
