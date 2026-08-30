{{/*
The OpenShift-injected CA bundle ConfigMap for ONE namespace. Shared by
ca-bundle.yaml beside this file and by the tenant template set, on the same
reasoning as _network-policies.tpl.
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
