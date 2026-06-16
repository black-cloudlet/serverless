{{- define "serverless-api.name" -}}
serverless-api
{{- end -}}

{{- define "serverless-api.labels" -}}
app.kubernetes.io/name: {{ include "serverless-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "serverless-api.clientCN" -}}
serverless-api.clients.{{ .Values.clientCert.baseDomain }}
{{- end -}}
