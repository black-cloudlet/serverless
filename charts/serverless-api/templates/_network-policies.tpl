{{/*
The workloads-namespace segmentation, defined once and rendered twice: into
the legacy namespace by networkpolicy.yaml beside this file, and into the
tenant template set by tenant-controller/configmap-network-policies.yaml. It
lives at the chart root, with the legacy render, because both use it - a
shared body under one component's folder would point the wrong way.
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
