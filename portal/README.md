# Cloud Console Portal

A self-service **web console** for the on-prem, airgapped platform - a single UI,
modelled on the **Google Cloud console**, that fronts the platform's growing set
of service APIs. The first offering is **Serverless** (the FaaS/CaaS API in the
sibling `serverless` project); more are added as data, not code.

Built with **Next.js (App Router) + TypeScript**. Identity is **SSO (Keycloak /
RHBK) OIDC** - the same realm and the same `groups` claim the Serverless API
trusts - so one login works across every offering, and group names are
**normalized identically** on both sides.

## What it does

- **Login with SSO (OIDC / RHBK).** Authorization Code + PKCE via a confidential
  Keycloak client. No local accounts. Access tokens are refreshed transparently
  and forwarded (server-side only) when the console calls a downstream API.
- **Group ("project") switcher, top-left.** A user can belong to several SSO
  groups; the console always operates in exactly one, chosen from the picker.
  The choice is validated against the token's group membership - it can never
  widen access.
- **Profile panel, top-right.** Name, username, email, the platform-admin badge,
  and the full group membership with the active group marked. A dedicated
  `/profile` page shows the same detail.
- **Service navigation, left rail.** The platform offerings grouped by category
  (GCP-style: _Serverless_, _Storage_, ...). Live services link to their page;
  not-yet-available ones show as _Coming soon_.
- **Serverless page.** Lists the active group's functions and containers from
  the Serverless API and shows the platform capabilities from its public
  `/info` endpoint.

## Group normalization

Ported verbatim from the Serverless API (`api/models/common.py::normalize_group`):
a group is stripped of the Keycloak path prefix (`/`) and a leading
`ggd-<1-4 digits>-` prefix, so `/ggd-1234-platforms` and `platforms` name the
same group. See `src/lib/groups.ts` (with tests in `tests/groups.test.ts`).

## Configuration

Everything dynamic is an environment variable (`PORTAL_*`), mirroring the
Serverless API's 12-factor config. In production the secrets are projected from
Vault via the External Secrets Operator (see `charts/portal`). See
[`.env.example`](.env.example) for the full list; the essentials:

| Variable                     | Purpose                                              |
| ---------------------------- | ---------------------------------------------------- |
| `AUTH_SECRET`                | Encrypts the session cookie (Auth.js).               |
| `AUTH_URL`                   | Public URL of the portal (OIDC redirect base).       |
| `PORTAL_OIDC_ISSUER`         | Keycloak realm issuer (shared with the APIs).        |
| `PORTAL_OIDC_CLIENT_ID`      | Confidential OIDC client id.                         |
| `PORTAL_OIDC_CLIENT_SECRET`  | OIDC client secret (from Vault via ESO).             |
| `PORTAL_OIDC_GROUPS_CLAIM`   | Token claim carrying groups (default `groups`).      |
| `PORTAL_ADMIN_GROUPS`        | JSON list of admin groups (same rule as the API).    |
| `PORTAL_SERVERLESS_API_URL`  | Address of the Serverless API.                       |
| `PORTAL_SERVICES`            | Optional JSON to add/override the service catalog.    |

Adding a new offering is env-only: point `PORTAL_SERVICES` (or a dedicated
`PORTAL_<NAME>_API_URL`) at the new API - no UI changes. See `src/lib/services.ts`.

## Layout

```
src/
  auth.ts             SSO/OIDC (Auth.js + Keycloak): tokens, refresh, groups
  middleware.ts       route gate (everything but /login + /api/auth is protected)
  lib/                groups (ported normalization), config, service catalog,
                      active-group cookie, Serverless API client
  app/
    login/            SSO sign-in landing
    (console)/        the shell: top bar (group switcher + profile) + side nav
      dashboard/      service cards
      serverless/     workloads for the active group
      profile/        full account detail
    api/              Auth.js endpoints, active-group setter, health probe
  components/         TopBar, GroupSwitcher, ProfileMenu, SideNav
charts/portal/        Helm chart (Deployment, Service, Route, ExternalSecret,
                      NetworkPolicy)
.github/workflows/    CI/CD: checks (reusable), ci, release
Dockerfile            multi-stage standalone Next.js build
```

## Develop

```bash
npm install
cp .env.example .env   # fill in AUTH_SECRET (openssl rand -base64 32) and OIDC client

npm run dev            # http://localhost:3000
npm run lint           # ESLint (next) + Prettier check via `npm run format:check`
npm run typecheck      # tsc --noEmit
npm test               # vitest
npm run build          # production build (standalone output)
```

> **Note:** this project lives under `portal/` in the `serverless` repository for
> now, but is fully self-contained (own `package.json`, `Dockerfile`, chart, and
> CI). It is intended to be extracted into its own repository; the CI workflows
> and chart already assume `portal/` is the repository root.

## Deploy

Helm chart in `charts/portal` (OpenShift `Route`, `Deployment`, `Service`,
`ExternalSecret`, default-deny `NetworkPolicy`), following the same conventions
as `charts/serverless-api`: image tag tracks the chart `appVersion`, secrets come
from Vault via ESO, and the internal CA bundle is trusted via `NODE_EXTRA_CA_CERTS`.
The release workflow builds/scans/signs the image and chart and cuts a GitHub
Release, exactly like the Serverless API's.
