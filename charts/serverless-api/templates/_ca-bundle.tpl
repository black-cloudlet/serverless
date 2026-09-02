{{/*
The OpenShift-injected CA bundle ConfigMap for ONE namespace. Rendered into
the API namespace by ca-bundle.yaml beside this file, and into the tenant
template set with the namespace token in place of a name.
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
