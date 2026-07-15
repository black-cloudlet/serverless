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
