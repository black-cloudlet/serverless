{{/*
Chart layout. Anything shared by more than one component stays in this
directory - the namespaces, the CA bundle, the identity the API and the build
controller share, the regions ConfigMap all three read, and these partials.
Everything a single component owns lives in that component's folder:

  api/               the REST API: Deployment, Service, Route, its own secrets
  build-controller/  the digest-propagation loop
  provisioner/       per-group namespaces: the loop, the ensure API, its own
                     identity, and the template set it applies
  kpack/             the build subsystem: builders, SCC, CA policy, credentials

Helm renders every file under templates/ regardless of depth and partials are
global, so the folders are for readers, not for Helm.
*/}}

{{- define "serverless-api.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Labels for a chart-created namespace: the standard chart labels, plus the shared
``namespaces.labels`` and a per-namespace override map (the latter wins on a key
clash). Call with a dict of the root context and the per-namespace labels:

  {{ include "serverless-api.namespaceLabels" (dict "root" $ "extra" $nsLabels) }}
*/}}
{{- define "serverless-api.namespaceLabels" -}}
{{ include "serverless-api.labels" .root }}
{{- $shared := .root.Values.namespaces.labels | default dict -}}
{{- range $k, $v := mergeOverwrite (deepCopy $shared) .extra }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end -}}

{{/*
The build controller's object name and selector labels. A distinct
``app.kubernetes.io/name`` keeps the API's Service from selecting its pods;
adding a component label to the API's own selector would fail every upgrade.
*/}}
{{- define "serverless-api.controllerName" -}}
{{ .Values.name }}-build-controller
{{- end -}}

{{- define "serverless-api.controllerLabels" -}}
app.kubernetes.io/name: {{ include "serverless-api.controllerName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Fully-qualified image reference for one deployment. Call with the root context
and that deployment's values:

  {{ include "serverless-api.image" (dict "root" $ "spec" $.Values.api) }}

Each deployment names its own ``repository``; the tag falls back to the shared
``image.tag`` and then to ``.Chart.AppVersion``, which CI stamps on a release, so
a released chart pins both images without a second version to keep in step.
*/}}
{{- define "serverless-api.image" -}}
{{- $root := .root -}}
{{- $repo := required "each deployment needs an image repository" .spec.repository -}}
{{- $tag := .spec.tag | default $root.Values.image.tag | default $root.Chart.AppVersion -}}
{{- with $root.Values.image.registry -}}
{{ . }}/{{ $repo }}:{{ $tag }}
{{- else -}}
{{ $repo }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/*
This release's own region entry, matched on `global.region`. Empty when the region is
not in `regions` - callers fall back to the platform defaults, as the API does.
*/}}
{{- define "serverless-api.region" -}}
{{- range .Values.regions -}}
{{- if eq .name $.Values.global.region -}}{{ toYaml . }}{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The registry THIS region pushes to and pulls from: its own `regions[].registry`
merged over the platform default. Matches CommonSettings.registry_for in
common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.regionRegistry" -}}
{{- $region := fromYaml (include "serverless-api.region" .) -}}
{{- $registry := deepCopy .Values.registry -}}
{{/* Key-present wins, key-absent inherits - including a present "", which is how
a registry says it has no namespacing path. NOT mergeOverwrite: it treats an empty
source value as unset, so `organization: ""` would silently inherit here while
CommonSettings.registry_for overrode. A nil is skipped, matching None-inherits. */}}
{{- range $k, $v := (default dict $region.registry) -}}
{{- if not (kindIs "invalid" $v) -}}{{- $_ := set $registry $k $v -}}{{- end -}}
{{- end -}}
{{- toYaml $registry -}}
{{- end -}}

{{/*
Registry host plus organization - the prefix every internal image hangs off.
Matches RegistryConfig.base in common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.registryBase" -}}
{{- $registry := fromYaml (include "serverless-api.regionRegistry" .) -}}
{{- $url := trimAll "/" $registry.url -}}
{{- with trimAll "/" (default "" $registry.organization) -}}
{{ $url }}/{{ . }}
{{- else -}}
{{ $url }}
{{- end -}}
{{- end -}}

{{/*
The ESO template that assembles one variable from the per-region token entries.
Values are interpolated as quoted JSON strings, so a token must not contain a
'"' or a '\' - a Quay OAuth token is alphanumeric.
*/}}
{{- define "serverless-api.regionTokensTemplate" -}}
{{- $entries := list -}}
{{- range .Values.regions -}}
{{- $entries = append $entries (printf "%q:\"%s\"" .name (printf `{{ index . %q }}` .name)) -}}
{{- end -}}
{{ printf "{%s}" (join "," $entries) }}
{{- end -}}

{{/*
The image a Builder composes and pushes.

  {{ include "serverless-api.builderImage" (dict "root" $ "name" "python") }}
*/}}
{{- define "serverless-api.builderImage" -}}
{{- printf "%s/%s/%s" (include "serverless-api.registryBase" .root) (tpl .root.Values.build.builderRepository .root) .name -}}
{{- end -}}

{{/*
A Builder's detection order, normalised to kpack's `spec.order`. Three forms, so
a real Paketo order can be pasted in verbatim:

  order: [paketo-buildpacks/go]                  one group, shorthand ids
  order: [{id: ..., optional: true}, ...]        one group, with flags
  order: [{group: [{id: ...}, ...]}, ...]        explicit groups, passed through

The multi-group form matters: collapsing groups by hand drops package managers.
*/}}
{{- define "serverless-api.builderOrder" -}}
{{- $order := . -}}
{{- $first := first $order -}}
{{- if and (kindIs "map" $first) (hasKey $first "group") -}}
{{- toYaml $order -}}
{{- else -}}
{{- $entries := list -}}
{{- range $order -}}
{{- if kindIs "string" . -}}
{{- $entries = append $entries (dict "id" .) -}}
{{- else -}}
{{- $entries = append $entries . -}}
{{- end -}}
{{- end -}}
{{- toYaml (list (dict "group" $entries)) -}}
{{- end -}}
{{- end -}}

{{/*
The runtimes file with build env resolved, so the API applies one list and needs
no merge logic. Precedence, lowest first: commonEnv, dependencyMirror, buildEnv.
*/}}
{{- define "serverless-api.resolvedRuntimes" -}}
{{- $root := . -}}
{{- $build := $root.Values.build | default dict -}}
{{- $shared := default (list) $build.commonEnv -}}
{{- with $build.dependencyMirror -}}
{{- $shared = append $shared (dict "name" "BP_DEPENDENCY_MIRROR" "value" .) -}}
{{- end -}}
{{- $out := list -}}
{{- range $root.Values.runtimes -}}
{{- $rt := deepCopy . -}}
{{- $env := concat $shared (default (list) $rt.buildEnv) -}}
{{- if $env -}}
{{- $_ := set $rt "buildEnv" $env -}}
{{- else -}}
{{- $_ := unset $rt "buildEnv" -}}
{{- end -}}
{{- $out = append $out $rt -}}
{{- end -}}
{{ dict "runtimes" $out | toYaml }}
{{- end -}}

{{/*
CA-trust env for build pods, pointed at the mounted bundle. Rendered into both the
policy's `initContainers` and `containers`. pip needs three names of its own: it
verifies against its vendored certifi, not the OS trust store or SSL_CERT_FILE
(docs/BUILDING.md - Trust: CA Injection).
*/}}
{{- define "serverless-api.buildCaEnv" -}}
{{- $path := . -}}
- name: SSL_CERT_FILE
  value: {{ $path | quote }}
- name: GIT_SSL_CAINFO
  value: {{ $path | quote }}
- name: NODE_EXTRA_CA_CERTS
  value: {{ $path | quote }}
- name: PIP_CERT
  value: {{ $path | quote }}
- name: REQUESTS_CA_BUNDLE
  value: {{ $path | quote }}
- name: CURL_CA_BUNDLE
  value: {{ $path | quote }}
{{- end -}}

{{/* api.route.timeout as a number of seconds ("65m" -> 3900). */}}
{{- define "serverless-api.routeTimeoutSeconds" -}}
{{- $t := .Values.api.route.timeout | toString -}}
{{- $n := regexFind "^[0-9]+" $t -}}
{{- $u := regexFind "[a-zA-Z]+$" $t -}}
{{- if not $n -}}
{{- fail (printf "serverless-api: api.route.timeout %q must be a number followed by s, m or h (e.g. \"65m\")." $t) -}}
{{- end -}}
{{- if eq $u "h" -}}{{ mul (atoi $n) 3600 }}
{{- else if eq $u "m" -}}{{ mul (atoi $n) 60 }}
{{- else if eq $u "s" -}}{{ atoi $n }}
{{- else -}}
{{- fail (printf "serverless-api: api.route.timeout %q must end in s, m or h (e.g. \"65m\")." $t) -}}
{{- end -}}
{{- end -}}

{{/* Fail fast when the router would cut streams before they end themselves.

A stream that runs to `stream.maxSeconds` and is severed by HAProxy at the
route timeout instead is the failure nobody diagnoses: the client just
reconnects, forever, losing whatever was mid-flight each time. The two values
live in different sections of values.yaml, so the relationship between them is
asserted here rather than left to whoever edits one of them. */}}
{{- define "serverless-api.validateStream" -}}
{{- $route := include "serverless-api.routeTimeoutSeconds" . | int -}}
{{- $stream := .Values.stream.maxSeconds | int -}}
{{- if le $route $stream -}}
{{- fail (printf "serverless-api: api.route.timeout (%s = %ds) must exceed stream.maxSeconds (%ds), or the router will cut every stream before it ends on its own." (.Values.api.route.timeout | toString) $route $stream) -}}
{{- end -}}
{{- if ge (.Values.stream.heartbeatSeconds | int) $route -}}
{{- fail (printf "serverless-api: stream.heartbeatSeconds (%d) must be well under api.route.timeout (%ds), or an idle stream is reaped between heartbeats." (.Values.stream.heartbeatSeconds | int) $route) -}}
{{- end -}}
{{- if gt (.Values.stream.minIntervalSeconds | int) (.Values.stream.maxIntervalSeconds | int) -}}
{{- fail "serverless-api: stream.minIntervalSeconds must not exceed stream.maxIntervalSeconds." -}}
{{- end -}}
{{- end -}}

{{/* Fail fast on build config that would render unusable manifests. */}}
{{- define "serverless-api.validateBuild" -}}
{{- if .Values.build.enabled -}}
{{- $names := list -}}
{{- range .Values.build.builders -}}
{{- $names = append $names .name -}}
{{- end -}}
{{- range .Values.runtimes -}}
{{- if not .builder -}}
{{- fail (printf "serverless-api: runtime %q has no `builder`; every runtime must map to a build.builders entry (or set build.enabled=false)." .name) -}}
{{- end -}}
{{- if not (has .builder $names) -}}
{{- fail (printf "serverless-api: runtime %q references builder %q, which is not defined in build.builders (%s)." .name .builder (join ", " $names)) -}}
{{- end -}}
{{- end -}}
{{- if not .Values.registry.url -}}
{{- fail "serverless-api: registry.url is required when build.enabled - builder and function images are pushed there." -}}
{{- end -}}
{{/* Only when a region actually names a registry: without one everything resolves
to the platform default anyway, so an unmatched global.region changes nothing. With
one, it silently keys this region's push credential to the WRONG host, which
surfaces as an unauthenticated push at the end of the first build. */}}
{{- $overridden := false -}}
{{- $names := list -}}
{{- range .Values.regions -}}
{{- $names = append $names .name -}}
{{- if .registry -}}{{- $overridden = true -}}{{- end -}}
{{- end -}}
{{- if and $overridden (not (has .Values.global.region $names)) -}}
{{- fail (printf "serverless-api: global.region %q is not one of `regions` (%s), so this release cannot tell which registry it builds into." .Values.global.region (join ", " $names)) -}}
{{- end -}}
{{/* The stack and store references are deliberately not checked here. They name
objects in another release, and kpack already reports a wrong or missing one on
the Builder's own status - a chart-side check could only repeat that, from a
chart that cannot see the cluster. */}}
{{- end -}}
{{- end -}}

{{/*
The two runtime facts the provisioner substitutes when it converges a tenant
namespace. Emitted as literal text, so what lands in the ConfigMap is the token
and not something Helm resolved (the same escaping the ESO templates use).
*/}}
{{- define "serverless-api.tenantNamespaceToken" -}}{{ `{{namespace}}` }}{{- end -}}
{{- define "serverless-api.tenantGroupToken" -}}{{ `{{group}}` }}{{- end -}}
{{- define "serverless-api.tenantRegionToken" -}}{{ `{{region}}` }}{{- end -}}
{{- define "serverless-api.tenantRegistryToken" -}}{{ `{{registry}}` }}{{- end -}}

{{/*
The tenant-templates ConfigMap's name, needed by both the ConfigMap itself and
the Deployment that mounts it.
*/}}
{{- define "serverless-api.tenantTemplatesName" -}}
{{ .Values.name }}-tenant-templates
{{- end -}}

{{/*
The provisioner's object name and selector labels, on the same reasoning as the
build controller's: a distinct ``app.kubernetes.io/name`` keeps the API's
Service from selecting its pods.
*/}}
{{- define "serverless-api.provisionerName" -}}
{{ .Values.name }}-provisioner
{{- end -}}

{{- define "serverless-api.provisionerLabels" -}}
app.kubernetes.io/name: {{ include "serverless-api.provisionerName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
A Vault path with this release's region put back as the provisioner's region
token, so the tenant template set stays byte-identical in every region and the
path is resolved against whichever cluster is being converged.

  {{ include "serverless-api.tenantVaultKey" (dict "root" $ "key" $key) }}
*/}}
{{- define "serverless-api.tenantVaultKey" -}}
{{- $root := .root -}}
{{- $resolved := tpl .key $root -}}
{{- $token := include "serverless-api.tenantRegionToken" $root -}}
{{- replace $root.Values.global.region $token $resolved -}}
{{- end -}}

{{/*
The segmentation for ONE workloads namespace. Rendered twice from this one body
- into the legacy namespace by networkpolicy.yaml, and into the tenant template
set with the namespace token in place of a name - so the two can never drift.
Call with the root context and the namespace to write into:

  {{ include "serverless-api.workloadNetworkPolicies" (dict "root" $ "namespace" $ns) }}
*/}}
{{- define "serverless-api.workloadNetworkPolicies" -}}
{{- $root := .root -}}
{{- $ns := .namespace -}}
# Additive segmentation for the workloads namespace: default-deny, then each
# allow-* reopens one path (Knative/OpenShift in; DNS/API/control-plane/off-cluster out).
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector: {} # every pod in the namespace
  policyTypes: [Ingress, Egress]
---
# Ingress: Knative + OpenShift only (same-namespace pods excluded -> no pod-to-pod).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-ingress-system
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector: {}
  policyTypes: [Ingress]
  ingress:
    - from:
        {{- range $root.Values.networkPolicy.ingressNamespaces }}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ . }}
        {{- end }}
---
# Egress: DNS resolution (openshift-dns).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-egress-dns
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Values.networkPolicy.dnsNamespace }}
      ports:
        {{- range $root.Values.networkPolicy.dnsPorts }}
        - protocol: UDP
          port: {{ . }}
        - protocol: TCP
          port: {{ . }}
        {{- end }}
---
# Egress: the platform API ("our side") and the Knative control plane.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-egress-internal
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ $root.Values.namespaces.api }}
        {{- range $root.Values.networkPolicy.egressNamespaces }}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ . }}
        {{- end }}
---
# Egress: off-cluster (LBs/Routes/external); internal CIDRs excluded so it can't
# reach other pods/Services directly.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-egress-external
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - to:
        - ipBlock:
            cidr: {{ $root.Values.networkPolicy.externalEgress.cidr }}
            {{- with $root.Values.networkPolicy.externalEgress.exceptCIDRs }}
            except:
              {{- toYaml . | nindent 14 }}
            {{- end }}
{{- $build := $root.Values.networkPolicy.build }}
{{- if and $root.Values.build.enabled $build.enabled }}
---
# Build pods only (`kpack.io/build`), not tenant pods. Additive: the default-deny
# and the tenant allowlist above are unchanged. docs/DEPLOYING.md - Network policy for build pods.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-build-pods
  namespace: {{ $ns }}
  labels:
    {{- include "serverless-api.labels" $root | nindent 4 }}
spec:
  podSelector:
    matchExpressions:
      - key: kpack.io/build
        operator: Exists
  policyTypes: [Ingress, Egress]
  {{- with $build.ingressNamespaces }}
  ingress:
    - from:
        {{- range . }}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ . }}
        {{- end }}
  {{- end }}
  {{- if or $build.egressNamespaces $build.egressCIDRs }}
  egress:
    {{- with $build.egressNamespaces }}
    - to:
        {{- range . }}
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: {{ . }}
        {{- end }}
    {{- end }}
    {{- with $build.egressCIDRs }}
    - to:
        {{- range . }}
        - ipBlock:
            cidr: {{ . }}
        {{- end }}
    {{- end }}
  {{- end }}
{{- end }}
{{- end -}}

{{/*
The OpenShift-injected CA bundle ConfigMap for ONE namespace. Same two-target
reason as the policies above.
*/}}
{{- define "serverless-api.caBundleConfigMap" -}}
{{- $root := .root -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $root.Values.caBundle.name }}
  namespace: {{ .namespace }}
  labels:
    config.openshift.io/inject-trusted-cabundle: "true"
    {{- include "serverless-api.labels" $root | nindent 4 }}
  annotations:
    argocd.argoproj.io/sync-options: Prune=false
{{- end -}}
