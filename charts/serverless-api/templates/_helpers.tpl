{{/*
Chart layout. Anything shared by more than one component stays in this
directory - the namespaces, the CA bundle, the identity the API and the build
controller share, the regions ConfigMap all three read, and these partials.
Everything a single component owns lives in that component's folder:

  api/               the REST API: Deployment, Service, Route, its own secrets
  build-controller/  the digest-propagation loop
  tenant-controller/ per-group namespaces: the loop, the provision API, its own
                     identity, and the template set it applies
  kpack/             the build subsystem: builders, SCC, CA policy, credentials

Helm renders every file under templates/ regardless of depth and partials are
global, so the folders are for readers, not for Helm.
*/}}

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
{{- define "serverless-api.buildControllerName" -}}
{{ .Values.name }}-build-controller
{{- end -}}

{{- define "serverless-api.buildControllerLabels" -}}
app.kubernetes.io/name: {{ include "serverless-api.buildControllerName" . }}
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
This release's own region entry, matched on `global.region`. Empty when the region is
not in `regions` - callers fall back to the platform defaults, as the API does.
*/}}
{{- define "serverless-api.region" -}}
{{- range .Values.regions -}}
{{- if eq .name $.Values.global.region -}}{{ toYaml . }}{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The registry THIS region pushes to and pulls from: its own `regions[].registry`
merged over the platform default. Matches CommonSettings.registry_for in
common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.regionRegistry" -}}
{{- $region := fromYaml (include "serverless-api.region" .) -}}
{{- $registry := deepCopy .Values.registry -}}
{{/* Key-present wins, key-absent inherits - including a present "", which is how
a registry says it has no namespacing path. NOT mergeOverwrite: it treats an empty
source value as unset, so `organization: ""` would silently inherit here while
CommonSettings.registry_for overrode. A nil is skipped, matching None-inherits. */}}
{{- range $k, $v := (default dict $region.registry) -}}
{{- if not (kindIs "invalid" $v) -}}{{- $_ := set $registry $k $v -}}{{- end -}}
{{- end -}}
{{- toYaml $registry -}}
{{- end -}}

{{/*
Registry host plus organization - the prefix every internal image hangs off.
Matches RegistryConfig.base in common/config.py; keep the two in step.
*/}}
{{- define "serverless-api.registryBase" -}}
{{- $registry := fromYaml (include "serverless-api.regionRegistry" .) -}}
{{- $url := trimAll "/" $registry.url -}}
{{- with trimAll "/" (default "" $registry.organization) -}}
{{ $url }}/{{ . }}
{{- else -}}
{{ $url }}
{{- end -}}
{{- end -}}

{{/*
The ESO template that assembles one variable from the per-region token entries.
Values are interpolated as quoted JSON strings, so a token must not contain a
'"' or a '\' - a Quay OAuth token is alphanumeric.
*/}}
{{- define "serverless-api.regionTokensTemplate" -}}
{{- $entries := list -}}
{{- range .Values.regions -}}
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

{{/* api.route.timeout as a number of seconds ("65m" -> 3900). */}}
{{- define "serverless-api.routeTimeoutSeconds" -}}
{{- $t := .Values.api.route.timeout | toString -}}
{{- $n := regexFind "^[0-9]+" $t -}}
{{- $u := regexFind "[a-zA-Z]+$" $t -}}
{{- if not $n -}}
{{- fail (printf "serverless-api: api.route.timeout %q must be a number followed by s, m or h (e.g. \"65m\")." $t) -}}
{{- end -}}
{{- if eq $u "h" -}}{{ mul (atoi $n) 3600 }}
{{- else if eq $u "m" -}}{{ mul (atoi $n) 60 }}
{{- else if eq $u "s" -}}{{ atoi $n }}
{{- else -}}
{{- fail (printf "serverless-api: api.route.timeout %q must end in s, m or h (e.g. \"65m\")." $t) -}}
{{- end -}}
{{- end -}}

{{/* Fail fast when the router would cut streams before they end themselves.

A stream that runs to `stream.maxSeconds` and is severed by HAProxy at the
route timeout instead is the failure nobody diagnoses: the client just
reconnects, forever, losing whatever was mid-flight each time. The two values
live in different sections of values.yaml, so the relationship between them is
asserted here rather than left to whoever edits one of them. */}}
{{- define "serverless-api.validateStream" -}}
{{- $route := include "serverless-api.routeTimeoutSeconds" . | int -}}
{{- $stream := .Values.stream.maxSeconds | int -}}
{{- if le $route $stream -}}
{{- fail (printf "serverless-api: api.route.timeout (%s = %ds) must exceed stream.maxSeconds (%ds), or the router will cut every stream before it ends on its own." (.Values.api.route.timeout | toString) $route $stream) -}}
{{- end -}}
{{- if ge (.Values.stream.heartbeatSeconds | int) $route -}}
{{- fail (printf "serverless-api: stream.heartbeatSeconds (%d) must be well under api.route.timeout (%ds), or an idle stream is reaped between heartbeats." (.Values.stream.heartbeatSeconds | int) $route) -}}
{{- end -}}
{{- if gt (.Values.stream.minIntervalSeconds | int) (.Values.stream.maxIntervalSeconds | int) -}}
{{- fail "serverless-api: stream.minIntervalSeconds must not exceed stream.maxIntervalSeconds." -}}
{{- end -}}
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
{{/* Only when a region actually names a registry: without one everything resolves
to the platform default anyway, so an unmatched global.region changes nothing. With
one, it silently keys this region's push credential to the WRONG host, which
surfaces as an unauthenticated push at the end of the first build. */}}
{{- $overridden := false -}}
{{- $names := list -}}
{{- range .Values.regions -}}
{{- $names = append $names .name -}}
{{- if .registry -}}{{- $overridden = true -}}{{- end -}}
{{- end -}}
{{- if and $overridden (not (has .Values.global.region $names)) -}}
{{- fail (printf "serverless-api: global.region %q is not one of `regions` (%s), so this release cannot tell which registry it builds into." .Values.global.region (join ", " $names)) -}}
{{- end -}}
{{/* The stack and store references are deliberately not checked here. They name
objects in another release, and kpack already reports a wrong or missing one on
the Builder's own status - a chart-side check could only repeat that, from a
chart that cannot see the cluster. */}}
{{- end -}}
{{- end -}}

{{/* Fail fast on backup config that would render a Schedule Trident Protect
refuses. It reports a bad one on the Schedule's own status, in a namespace
nobody is watching, so the first anyone would hear of it is a restore that has
nothing to restore from. */}}
{{- define "serverless-api.validateBackup" -}}
{{- if .Values.backup.enabled -}}
{{- if not .Values.backup.appVault.name -}}
{{- fail "serverless-api: backup.appVault.name is required when backup.enabled - a Schedule with no AppVault has nowhere to write its copies. It names an AppVault the storage administrator declares on the cluster; this chart never creates one." -}}
{{- end -}}
{{/* The name reaches the tenant namespace verbatim, so the ONLY templating it
may carry is the tenant controller's own region token. Anything else - a Helm
expression, which values.yaml is not rendered through - would land in the
Schedule as literal braces and be reported nowhere this release can see. */}}
{{- $token := include "serverless-api.tenantRegionToken" . -}}
{{- if contains "{{" (replace $token "" .Values.backup.appVault.name) -}}
{{- fail (printf "serverless-api: backup.appVault.name %q may only carry %s, which the tenant controller resolves per cluster; values.yaml is not a template, so anything else reaches the Schedule as literal text." .Values.backup.appVault.name $token) -}}
{{- end -}}
{{- if not .Values.backup.schedules -}}
{{- fail "serverless-api: backup.enabled with no backup.schedules would declare the application and never back it up. Set backup.enabled=false, or give it at least one schedule." -}}
{{- end -}}
{{/* Which time fields each granularity requires; without them it never fires. */}}
{{- $required := dict "Hourly" (list "minute") "Daily" (list "minute" "hour") "Weekly" (list "minute" "hour" "dayOfWeek") "Monthly" (list "minute" "hour" "dayOfMonth") -}}
{{- $seen := list -}}
{{- range .Values.backup.schedules -}}
{{- $schedule := . -}}
{{- if not $schedule.name -}}
{{- fail "serverless-api: every backup.schedules entry needs a `name`; it is the suffix of the Schedule object's own name." -}}
{{- end -}}
{{- if has $schedule.name $seen -}}
{{- fail (printf "serverless-api: two backup.schedules entries are named %q; they would render one Schedule, and the second would overwrite the first." $schedule.name) -}}
{{- end -}}
{{- $seen = append $seen $schedule.name -}}
{{- $granularity := $schedule.granularity | toString -}}
{{- if not (hasKey $required $granularity) -}}
{{- fail (printf "serverless-api: backup schedule %q has granularity %q; Trident Protect takes one of %s." $schedule.name $granularity (join ", " (keys $required | sortAlpha))) -}}
{{- end -}}
{{/* Set, not merely present: `minute: ""` is how NetApp's own examples write a
field a granularity does not use, and it would render a Schedule with no time to
fire at. `dig`'s default covers the absent case, `not` the empty one, and both
leave a legitimate "0" or 0 alone. */}}
{{- range $field := index $required $granularity -}}
{{- if not (dig $field "" $schedule | toString) -}}
{{- fail (printf "serverless-api: backup schedule %q is %s, so it needs `%s` set; without it the Schedule has no time to run at." $schedule.name $granularity $field) -}}
{{- end -}}
{{- end -}}
{{- if not (dig "backupRetention" "" $schedule | toString) -}}
{{- fail (printf "serverless-api: backup schedule %q has no `backupRetention`; how many copies to keep is the one thing a schedule cannot default." $schedule.name) -}}
{{- end -}}
{{- if not (dig "snapshotRetention" $.Values.backup.snapshotRetention $schedule | toString) -}}
{{- fail (printf "serverless-api: backup schedule %q resolves `snapshotRetention` to nothing; set it on the schedule, or set backup.snapshotRetention (\"0\" keeps no snapshot)." $schedule.name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
The tenant controller's object name and selector labels, on the same reasoning as the
build controller's: a distinct ``app.kubernetes.io/name`` keeps the API's
Service from selecting its pods.
*/}}
{{- define "serverless-api.tenantControllerName" -}}
{{ .Values.name }}-tenant-controller
{{- end -}}

{{- define "serverless-api.tenantControllerLabels" -}}
app.kubernetes.io/name: {{ include "serverless-api.tenantControllerName" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
