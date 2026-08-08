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
This release's own site entry, matched on `global.site`. Empty when the site is
not in `sites` - callers fall back to the platform defaults, as the API does.
*/}}
{{- define "serverless-api.site" -}}
{{- range .Values.sites -}}
{{- if eq .name $.Values.global.site -}}{{ toYaml . }}{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The registry THIS site pushes to and pulls from: its own `sites[].registry`
merged over the platform default. Matches CommonSettings.registry_for in
common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.siteRegistry" -}}
{{- $site := fromYaml (include "serverless-api.site" .) -}}
{{- $registry := deepCopy .Values.registry -}}
{{/* Key-present wins, key-absent inherits - including a present "", which is how
a registry says it has no namespacing path. NOT mergeOverwrite: it treats an empty
source value as unset, so `organization: ""` would silently inherit here while
CommonSettings.registry_for overrode. A nil is skipped, matching None-inherits. */}}
{{- range $k, $v := (default dict $site.registry) -}}
{{- if not (kindIs "invalid" $v) -}}{{- $_ := set $registry $k $v -}}{{- end -}}
{{- end -}}
{{- toYaml $registry -}}
{{- end -}}

{{/*
Registry host plus organization - the prefix every internal image hangs off.
Matches RegistryConfig.base in common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.registryBase" -}}
{{- $registry := fromYaml (include "serverless-api.siteRegistry" .) -}}
{{- $url := trimAll "/" $registry.url -}}
{{- with trimAll "/" (default "" $registry.organization) -}}
{{ $url }}/{{ . }}
{{- else -}}
{{ $url }}
{{- end -}}
{{- end -}}

{{/*
The ESO template that assembles one variable from the per-site token entries.
Values are interpolated as quoted JSON strings, so a token must not contain a
'"' or a '\' - a Quay OAuth token is alphanumeric.
*/}}
{{- define "serverless-api.siteTokensTemplate" -}}
{{- $entries := list -}}
{{- range .Values.sites -}}
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
{{/* Only when a site actually names a registry: without one everything resolves
to the platform default anyway, so an unmatched global.site changes nothing. With
one, it silently keys this site's push credential to the WRONG host, which
surfaces as an unauthenticated push at the end of the first build. */}}
{{- $overridden := false -}}
{{- $names := list -}}
{{- range .Values.sites -}}
{{- $names = append $names .name -}}
{{- if .registry -}}{{- $overridden = true -}}{{- end -}}
{{- end -}}
{{- if and $overridden (not (has .Values.global.site $names)) -}}
{{- fail (printf "serverless-api: global.site %q is not one of `sites` (%s), so this release cannot tell which registry it builds into." .Values.global.site (join ", " $names)) -}}
{{- end -}}
{{/* The stack and store references are deliberately not checked here. They name
objects in another release, and kpack already reports a wrong or missing one on
the Builder's own status - a chart-side check could only repeat that, from a
chart that cannot see the cluster. */}}
{{- end -}}
{{- end -}}
