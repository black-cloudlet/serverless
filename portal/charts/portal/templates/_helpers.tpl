{{- define "portal.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Fully-qualified container image reference.

The tag falls back to the chart's ``.Chart.AppVersion`` when ``image.tag`` is
left empty, so a released chart pins the matching image without setting the
version in two places (CI stamps ``appVersion`` to the git tag on a release and
leaves it "latest" on main). An optional ``image.registry`` is prefixed when set.
*/}}
{{- define "portal.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- with .Values.image.registry -}}
{{ . }}/{{ $.Values.image.repository }}:{{ $tag }}
{{- else -}}
{{ .Values.image.repository }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/*
The portal's public URL (Auth.js AUTH_URL / OIDC redirect base). Explicit
``publicUrl`` wins; otherwise it is https:// the Route host, which itself
defaults to portal.{global.baseDomain}.
*/}}
{{- define "portal.publicUrl" -}}
{{- if .Values.publicUrl -}}
{{ .Values.publicUrl }}
{{- else -}}
{{- $host := .Values.route.host | default (printf "portal.%s" .Values.global.baseDomain) -}}
https://{{ $host }}
{{- end -}}
{{- end -}}
