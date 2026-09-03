{{/*
The two runtime facts the tenant controller substitutes when it converges a tenant
namespace. Emitted as literal text, so what lands in the ConfigMap is the token
and not something Helm resolved (the same escaping the ESO templates use).
*/}}
{{- define "serverless-api.tenantNamespaceToken" -}}{{ `{{namespace}}` }}{{- end -}}
{{- define "serverless-api.tenantGroupToken" -}}{{ `{{group}}` }}{{- end -}}
{{- define "serverless-api.tenantRegionToken" -}}{{ `{{region}}` }}{{- end -}}
{{- define "serverless-api.tenantRegistryToken" -}}{{ `{{registry}}` }}{{- end -}}

{{/*
The template set is shipped as one ConfigMap per resource group rather than one
big one, so each is reviewed as the thing it configures. The Deployment projects
them all into a single directory, which is what the controller reads and hashes -
so the split is a chart-side concern the running service never sees.

This list is the join between the two: each name below must have a
configmap-{part}.yaml beside this file, and a test asserts that the Deployment
projects nothing the chart does not render.
*/}}
{{- define "serverless-api.tenantTemplateParts" -}}
namespace ca-bundle rbac
{{- if .Values.networkPolicy.enabled }} network-policies{{ end }}
{{- if .Values.build.enabled }} build{{ end }}
{{- end -}}

{{/*
One part's ConfigMap name.

  {{ include "serverless-api.tenantTemplatesName" (dict "root" $ "part" "rbac") }}
*/}}
{{- define "serverless-api.tenantTemplatesName" -}}
{{ .root.Values.name }}-tenant-{{ .part }}
{{- end -}}

{{/*
A Vault path with this release's region put back as the tenant controller's region
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
