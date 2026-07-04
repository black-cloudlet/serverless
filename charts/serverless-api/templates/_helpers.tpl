{{- define "serverless-api.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
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
