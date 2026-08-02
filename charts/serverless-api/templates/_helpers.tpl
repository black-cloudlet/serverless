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
Fully-qualified container image reference.

The tag falls back to the chart's ``.Chart.AppVersion`` when ``image.tag`` is
left empty, so a released chart pins the matching image without setting the
version in two places: CI stamps ``appVersion`` to the git tag on a release (and
leaves it "latest" on main), and this image tracks it automatically. An optional
``image.registry`` is prefixed when set.
*/}}
{{- define "serverless-api.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- with .Values.image.registry -}}
{{ . }}/{{ $.Values.image.repository }}:{{ $tag }}
{{- else -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/*
Registry host plus organization - the prefix every internal image hangs off.
Matches RegistryConfig.base in common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.registryBase" -}}
{{- $url := trimAll "/" .Values.registry.url -}}
{{- with trimAll "/" (default "" .Values.registry.organization) -}}
{{ $url }}/{{ . }}
{{- else -}}
{{ $url }}
{{- end -}}
{{- end -}}

{{/*
The image a Builder composes and pushes.

  {{ include "serverless-api.builderImage" (dict "root" $ "name" "python") }}
*/}}
{{- define "serverless-api.builderImage" -}}
{{- printf "%s/%s/%s" (include "serverless-api.registryBase" .root) .root.Values.build.builderRepository .name -}}
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
{{/* The stack and store references are deliberately not checked here. They name
objects in another release, and kpack already reports a wrong or missing one on
the Builder's own status - a chart-side check could only repeat that, from a
chart that cannot see the cluster. */}}
{{- end -}}
{{- end -}}
