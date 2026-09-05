// One content model for both renderers. 7x7 rule: <= 7 lines per slide, <= 7 words per line.
// Visual coordinates live in a 600 x 440 box.
//
// reveal: how the visual animates.
//   "auto"   (default) the whole visual builds itself when the slide opens
//   "paired" visual element k appears together with line k
//   "rows"   visual elements appear one per click, after all the lines
//   "after"  the whole visual appears on one click, after all the lines
//
// textSize: "lede" (one or two big sentences) or "small" (fuller sentences).
// logo: a key in logos.json, drawn top-right.
// clickYaml: in the web deck, clicking a node opens its desc / yaml.

const N = (id, x, y, w, h, label, sub, tone, yaml, desc) => ({ id, x, y, w, h, label, sub, tone: tone || "teal", yaml, desc });
const E = (from, to, opts) => ({ from, to, ...(opts || {}) });

const slides = [
  // ---------------- 1
  { kind: "title", kicker: "Cloudlet's Serverless", title: "From Knative to the Portal", sub: "What we built, and why.", meta: "Team review · September 2026",
    notes: "Open with the one-sentence promise: a developer gives us code or an image and gets a running, addressable service in both regions without ever seeing Kubernetes." },
  // ---------------- 2
  { kind: "content", kicker: "Today", title: "What we'll talk about",
    lines: ["What is serverless computing", "What is Knative and OpenShift Serverless Operator", "The API: active-active Knative wrapper", "Functions: buildpacks, kpack, build controller", "The portal and cloudlet-apis", "Where we go next"],
    visual: { kind: "glyph", text: "SLIDES", sub: "slides · six topics · one platform" },
    notes: "Foundations is the vocabulary. The API is the contract. Functions and builds is where most of the engineering went. Portal and future is what the platform becomes next." },

  // ================ FOUNDATIONS
  // ---------------- 3  definition, deliberately two sentences
  { kind: "content", kicker: "Foundations", title: "Serverless, defined", textSize: "lede",
    lines: ["The servers are still there — you stop owning them.", "You hand over code. The platform runs it only while work arrives."],
    visual: { kind: "glyph", text: "serverless ≠ no servers", sub: "someone else's problem, on purpose" },
    notes: "Definition first, before any properties, and deliberately only two sentences. Serverless does not mean there is no server; it means the team shipping the code does not own the machine, the capacity, or the scaling. The unit you hand over is code or an image, not a host. The platform places it, starts it on demand and stops it when idle, and the bill follows the work rather than the clock." },
  // ---------------- 4  properties
  { kind: "content", kicker: "Foundations", title: "What that buys you",
    lines: ["Scale to zero when idle", "Scale out when traffic arrives", "Pay for requests, not hours", "Nothing to patch, nothing to plan", "One deploy, no capacity meeting"],
    visual: { kind: "glyph", text: "0 → N → 0", sub: "replicas follow traffic" },
    notes: "These are the consequences of the definition. Scale to zero is the defining property: an idle workload costs nothing and needs nobody. Scaling out is automatic. Patching the base and the runtime is the platform's job, so nobody plans capacity for a function." },
  // ---------------- 5  trade-offs, table only
  { kind: "content", kicker: "Foundations", title: "Serverless: the trade-offs", wide: true, reveal: "rows",
    lines: [],
    visual: { kind: "table", plain: true, head: ["", "Pro", "Con"], rows: [
      ["Cost", "Pay only when it runs", "Bills spike under load"],
      ["Ops", "No servers to patch", "Runtime is not yours"],
      ["Scale", "Up and down by itself", "Cold start on first hit"],
      ["Speed", "A function in minutes", "Platform lock-in"],
      ["Fit", "Bursty, event-driven", "Long-running jobs, state"],
      ["Deploy", "Easy, few clicks", "Less control and customization"],
    ] },
    notes: "Be honest about the trade-offs before the architecture, one row per click. Cost: nothing when idle, but a hot loop of requests is billed per request. Ops: no patching, but you cannot tune the kernel or the runtime. Scale: automatic in both directions, but the first request after idle pays a cold start. Speed: a function ships in minutes, but the deployment model is the platform's. Fit: request-driven and bursty workloads win; long-running or stateful jobs do not belong here. Deploy: a few clicks to ship, at the cost of control over how it is built and run." },
  // ---------------- 6  Knative: what it is and where it came from
  { kind: "content", kicker: "Foundations", title: "Knative, and where it came from", textSize: "small", logo: "knative",
    lines: [
      "Google open-sourced Knative in 2018; it joined the CNCF in 2022.",
      "It is not a platform of its own — it is serverless building blocks for Kubernetes.",
      "Serving handles requests, Eventing routes events. We run Serving only.",
    ],
    visual: { kind: "stack", layers: [
      ["2018", "Google open-sources Knative", "accent"],
      ["2022", "donated to the CNCF"],
      ["Serving", "request-driven workloads"],
      ["Eventing", "brokers, triggers, CloudEvents"],
      ["Ours", "Serving, via the Operator"],
    ] },
    notes: "Knative was opened up by Google in 2018 and became a CNCF incubating project in 2022, so it is not a vendor-only technology. It is not a platform in itself: it is a set of Kubernetes building blocks. Serving covers request-driven workloads, Eventing covers event routing with brokers and triggers. We run Serving; Eventing exists but we do not expose it." },
  // ---------------- 7  from what we already run
  { kind: "content", kicker: "Foundations", title: "Knative, from what we already run", reveal: "rows",
    lines: ["We know Deployment, Service, Ingress, HPA", "Knative bundles them into one object", "Adds revisions, routing and scale-to-zero", "Runs on any Kubernetes, ours is OpenShift"],
    visual: { kind: "table", head: ["Need", "Plain Kubernetes", "Knative"], rows: [
      ["Run the code", "Deployment", "Revision"],
      ["Reach it", "Service + Ingress", "Route"],
      ["Scale it", "HPA, never to zero", "KPA, down to zero"],
      ["Change it", "Rolling update", "New Revision, old kept"],
      ["Own hostname", "Ingress rule", "DomainMapping"],
    ] },
    notes: "Start from the objects the team writes every day, then bring the table up a row at a time after the points. A Deployment runs the code, a Service plus an Ingress reaches it, an HPA scales it but never below one, a rolling update replaces it. Knative Serving covers the same needs with one object and adds what plain Kubernetes lacks: immutable revisions you can route between, and scaling to zero." },
  // ---------------- 8  the object; each block with its line; YAML on click
  { kind: "content", kicker: "Foundations", title: "One object: the Knative Service", reveal: "paired", clickYaml: true,
    hint: "click any resource for its YAML",
    lines: ["The Service owns Configuration and Route", "Configuration holds the desired code", "Every change is an immutable Revision", "Route sends traffic to revisions", "DomainMapping gives it your hostname"],
    visual: { kind: "graph", nodes: [
      N("svc", 170, 10, 260, 64, "Knative Service", "the one object you write", "accent",
        "apiVersion: serving.knative.dev/v1\nkind: Service\nmetadata:\n  name: billing\n  namespace: team-a-serverless\nspec:\n  template:\n    spec:\n      containers:\n        - image: registry/billing@sha256:...",
        "The only object the API writes. It owns a Configuration and a Route, so one document describes the code and how traffic reaches it."),
      N("cfg", 20, 140, 240, 64, "Configuration", "desired state of the code", "teal",
        "kind: Configuration\nspec:\n  template:\n    metadata:\n      annotations:\n        autoscaling.knative.dev/min-scale: \"0\"\n        autoscaling.knative.dev/max-scale: \"10\"\n    spec:\n      containers:\n        - image: registry/billing@sha256:...",
        "The desired state of the code: image, env, port, scaling annotations. Editing it stamps a new Revision."),
      N("rev", 20, 270, 240, 64, "Revision N", "immutable snapshot", "teal",
        "kind: Revision\nmetadata:\n  name: billing-00007\nspec:\n  containers:\n    - image: registry/billing@sha256:...\nstatus:\n  conditions:\n    - type: Ready\n      status: \"True\"",
        "An immutable snapshot of one Configuration. It never changes; a new deploy creates the next one and the old stays addressable."),
      N("rt", 340, 140, 240, 64, "Route", "traffic → revisions", "teal",
        "kind: Route\nspec:\n  traffic:\n    - revisionName: billing-00007\n      percent: 100\nstatus:\n  url: http://billing.team-a-serverless.svc",
        "Decides which revisions receive traffic and in what split. This is where a canary or an instant rollback would live."),
      N("dm", 340, 270, 240, 64, "DomainMapping", "your own hostname", "teal",
        "apiVersion: serving.knative.dev/v1beta1\nkind: DomainMapping\nmetadata:\n  name: billing-team-a.serverless.example\nspec:\n  ref:\n    kind: Service\n    name: billing",
        "Attaches a hostname of our choosing to the Route. This is how one workload answers on the same address in both regions."),
    ], edges: [E("svc", "cfg"), E("cfg", "rev"), E("svc", "rt"), E("rt", "rev", { dashed: true }), E("dm", "rt", { dashed: true })] },
    notes: "Each block lands with the line that explains it. A Service owns a Configuration and a Route. Every change to the Configuration stamps a new immutable Revision; old ones stay addressable. The Route decides which revisions receive traffic and in what split. A DomainMapping attaches a hostname of your choosing, which is how we give one workload the same address in both regions. In the web deck, click any block to see the YAML for that resource." },
  // ---------------- 9  the full example, big
  { kind: "content", kicker: "Foundations", title: "The Service, in one file", wide: true, reveal: "after",
    lines: [],
    visual: { kind: "code", lines: [
      ["apiVersion: serving.knative.dev/v1", "accent"],
      ["kind: Service", "accent"],
      ["metadata:", "ink"],
      ["  name: billing", "muted"],
      ["  namespace: team-a-serverless          # one namespace per SSO group", "muted"],
      ["spec:", "ink"],
      ["  template:", "muted"],
      ["    metadata:", "muted"],
      ["      annotations:", "muted"],
      ["        autoscaling.knative.dev/metric: concurrency", "muted"],
      ["        autoscaling.knative.dev/target: \"80\"", "muted"],
      ["        autoscaling.knative.dev/min-scale: \"0\"      # scale to zero", "muted"],
      ["        autoscaling.knative.dev/max-scale: \"10\"", "muted"],
      ["    spec:", "muted"],
      ["      containers:", "muted"],
      ["        - image: registry.central/team-a/billing@sha256:9f2c...", "muted"],
      ["          ports:", "muted"],
      ["            - containerPort: 8080", "muted"],
      ["          resources:", "muted"],
      ["            requests: { cpu: 250m, memory: 512Mi }", "muted"],
    ] },
    notes: "The whole object on one slide, so nobody has to imagine it. Two lines of apiVersion and kind, a name and a namespace, and then a pod template with autoscaling annotations. The annotations are the only Knative-specific part: which metric drives scaling, the target per pod, and the floor and ceiling. min-scale zero is what makes it scale to zero. Everything below spec.template.spec is an ordinary pod spec. The API generates exactly this, with the image pinned by digest." },
  // ---------------- 10 the pieces, before the flow
  { kind: "content", kicker: "Foundations", title: "The pieces that do the scaling", reveal: "rows",
    lines: ["Concurrency: requests in flight per pod", "The queue proxy measures it", "The KPA turns that into replicas", "The Activator stands in at zero", "minScale and maxScale bound it"],
    visual: { kind: "table", plain: true, head: ["Piece", "What it does", "Role"], rows: [
      ["Queue proxy", "Counts in-flight requests", "the meter"],
      ["KPA", "Requests → replica count", "the decision"],
      ["Activator", "Holds requests while pods start", "the safety net"],
      ["minScale", "Keeps N pods warm", "the override"],
      ["maxScale", "Caps the fan-out", "the ceiling"],
    ] },
    notes: "Explain the vocabulary before showing the flow. Every pod runs a queue proxy sidecar that reports how many requests are in flight. The Knative Pod Autoscaler reads that and decides the replica count. The Activator is the component that sits in the request path when a revision is at zero, so a request is never dropped while pods start. minScale keeps pods warm for latency-sensitive workloads; maxScale is the ceiling the API sets." },
  // ---------------- 11 the flow; one revision box whose state changes
  { kind: "content", kicker: "Foundations", title: "Scaling to zero, and back", reveal: "paired",
    lines: ["An idle revision sits at zero pods", "A request arrives for that revision", "The Activator holds it, never drops it", "The KPA is asked for capacity", "Pods start, the request is served", "Once healthy, traffic goes direct"],
    visual: { kind: "graph", nodes: [
      N("rev", 140, 340, 320, 84, "Revision", "0 pods · no cost"),
      N("req", 10, 30, 230, 70, "request", "GET /billing", "accent"),
      N("act", 330, 30, 250, 70, "Activator", "buffers, never drops", "accent"),
      N("kpa", 330, 185, 250, 70, "KPA", "0 → 1 replica"),
      N("rev2", 140, 340, 320, 84, "Revision", "starting · 1 pod"),
      N("rev3", 140, 340, 320, 84, "Revision", "Ready · traffic direct"),
    ], edges: [E("req", "act"), E("act", "kpa"), E("kpa", "rev")] },
    notes: "Now the flow, one box per line, and watch the bottom box: it is the same revision the whole way through, only its state changes. An idle revision holds zero pods. A request arrives and the Activator, which stands in the data path at zero, buffers it rather than dropping it. It asks the KPA for capacity, pods start, and the request is served. Once the revision reports healthy the Activator steps out and traffic goes straight to the pods." },
  // ---------------- 12
  { kind: "content", kicker: "Foundations", title: "The OpenShift Serverless Operator",
    lines: ["Red Hat's supported Knative distribution", "One KnativeServing custom resource", "OLM installs it and upgrades it", "Kourier ingress, Routes created for you", "Catalog mirrors with oc-mirror"],
    visual: { kind: "stack", layers: [["Knative Serving", "Service · Revision · Route · DomainMapping", "accent"], ["KnativeServing CR", "one document"], ["Serverless Operator", "reconciles, upgrades"], ["Operator Lifecycle Manager", "Subscription → catalog"], ["OpenShift", "one cluster per region"]] },
    notes: "The operator is Red Hat's packaging of Knative. One KnativeServing CR, reconciled by the operator, upgraded through OLM. Kourier is the ingress; Routes are created for us. The catalog mirrors with oc-mirror, which is what makes it viable in an airgap." },
  // ---------------- 13 the decision, table only
  { kind: "content", kicker: "Foundations", title: "Why the Operator", wide: true, reveal: "rows",
    lines: [],
    visual: { kind: "table", head: ["", "Upstream Knative", "Serverless Operator"], rows: [
      ["Upgrades", "YAML we maintain by hand", "OLM, from the catalog"],
      ["Ingress", "We run and tune it", "Kourier, managed"],
      ["Routes, TLS", "Hand-made, extra RBAC", "Created for us"],
      ["Airgap", "Image by image", "oc-mirror, one path"],
      ["Support", "Community", "Red Hat"],
      ["Who owns it", "Us", "The platform"],
    ] },
    notes: "The decision is about ownership, one row per click. Upstream would mean maintaining the install, the ingress, the Routes and the mirror ourselves, with community support. The chart assumes the operator's conventions and the API holds no routes RBAC. Recorded in docs/DEPLOYING.md and the locked decisions in docs/ARCHITECTURE.md." },

  // ================ THE API
  // ---------------- 14
  { kind: "section", title: "The API", sub: "One HTTP call, a serverless application." },
  // ---------------- 15 what the API is
  { kind: "content", kicker: "The API", title: "What the API is", reveal: "paired",
    lines: ["One HTTP API in front of Knative", "Two offerings: functions and containers", "It validates, then answers 202", "One call reaches both regions", "Nobody needs cluster access"],
    visual: { kind: "stack", layers: [
      ["one API", "FastAPI, running in both regions", "accent"],
      ["functions", "your git repo — we build it"],
      ["containers", "your image — we run it"],
      ["the contract", "202 + statusUrl, closed statuses"],
      ["the reach", "central + south, one call"],
    ] },
    notes: "This is the whole API in five sentences. It is a FastAPI control plane that speaks HTTP and writes Knative, so nobody outside the platform team touches a cluster. Two offerings share one body shape: a function is source we build, a container is an image you bring. Every write validates synchronously, then returns 202 with a statusUrl. One call fans out to both regions." },
  // ---------------- 16
  { kind: "content", kicker: "The API", title: "The endpoint structure",
    lines: ["23 endpoints under one base path", "The group is in the path", "Both offerings have the same shape", "OIDC token or admin key", "Allow live streaming with short-lived tickets"],
    visual: { kind: "code", lines: [
      ["/api/serverless/v1", "accent"],
      ["  /groups/{group}/functions", "ink"],
      ["    POST · GET · PUT · DELETE", "muted"],
      ["    /{name}/build", "muted"],
      ["    /{name}/stats · /pods · /logs", "muted"],
      ["  /groups/{group}/containers", "ink"],
      ["    POST · GET · PUT · DELETE", "muted"],
      ["    /{name}/pull", "muted"],
      ["    /{name}/stats · /pods · /logs", "muted"],
      ["  /functions/info · /containers/info", "ink"],
      ["  /stream-tickets", "ink"],
    ] },
    notes: "Base path /api/serverless/v1. The SSO group is a path segment, so authorization is a path check against the groups claim. Both offerings carry the same ten endpoints; the only difference is that a function has /build where a container has /pull. Two public info endpoints and one ticket mint sit outside the group. The Authorization header carries either a Keycloak JWT or the static admin key. EventSource cannot send headers, hence the 60-second HMAC ticket." },
  // ---------------- 17
  { kind: "content", kicker: "The API", title: "Anatomy of a workload", reveal: "paired",
    lines: ["Identity: name, hostname, regions", "Source: git repo or image", "Runtime: env, files, port, size", "Scaling: min, max, metric, target", "Response: same shape, secrets redacted"],
    visual: { kind: "stack", layers: [
      ["Identity", "name · hostname · regions[]", "accent"],
      ["Source", "gitRepo · branch · runtime  |  image"],
      ["Runtime", "env[] · files[] · port · size"],
      ["Scaling", "minScale · maxScale · metric · target"],
      ["Response", "the request, secrets redacted"],
    ] },
    notes: "One body shape for both offerings, one band per line. Sizes map to CPU request-only and memory request equals limit. Concurrency and rps use the KPA and can scale to zero; cpu and memory switch to HPA. PUT is a full replace but keeps redacted secrets when omitted, so a read-modify-write never strips credentials." },
  // ---------------- 18 statuses
  { kind: "content", kicker: "The API", title: "Every workload has one status", reveal: "rows",
    lines: ["A closed set, published on /info", "Pending, Building, Deploying, Ready", "Building only exists for functions", "Failed always carries a reason", "One row per region, rolled up"],
    visual: { kind: "lifecycle", phases: ["Pending", "Building", "Deploying", "Ready"], failed: ["BuildFailed", "ImagePullFailed", "CrashLooping", "ConfigError", "ProgressDeadlineExceeded"] },
    notes: "The status vocabulary is closed and published on /info so no client hardcodes it. Walk the happy path one chip at a time, then the failure path. Building only appears for functions, because only a function has a build. Terminating follows a delete. Failed always carries a machine-readable reason next to the human message, and every region reports its own status which the API rolls up into one." },
  // ---------------- 19 flow 1 of 3: the request
  { kind: "content", kicker: "The API · 1 of 3 · the request", title: "You send this", wide: true, reveal: "after",
    lines: [],
    visual: { kind: "code", lines: [
      ["POST /api/serverless/v1/groups/team-a/functions", "accent"],
      ["Authorization: Bearer <keycloak token>", "ink"],
      ["", "muted"],
      ["{", "ink"],
      ["  \"name\": \"billing\",  \"port\": 8080,  \"size\": \"small\",", "muted"],
      ["  \"gitRepo\": \"https://git.internal/team-a/billing.git\",", "muted"],
      ["  \"branch\": \"main\",  \"runtime\": \"python\",  \"version\": \"3.12\",", "muted"],
      ["  \"regions\": [\"central\", \"south\"],", "muted"],
      ["  \"env\": [{ \"name\": \"LOG_LEVEL\", \"value\": \"info\" }],", "muted"],
      ["  \"scaling\": { \"minScale\": 0, \"maxScale\": 10,", "muted"],
      ["                \"metric\": \"concurrency\", \"target\": 80 }", "muted"],
      ["}", "ink"],
      ["", "muted"],
      ["# no namespace, no image, no Knative, no kubectl", "accent"],
    ] },
    notes: "Step one of the interaction. A real create: a name, where the source lives, which runtime, which regions, and how it should scale. Note what the caller does not send: no namespace, no image, no Knative object, no cluster credentials. The token in the Authorization header carries the groups claim, and team-a in the path must be one of them." },
  // ---------------- 20 flow 2 of 3: the 202
  { kind: "content", kicker: "The API · 2 of 3 · the answer", title: "You get this back, immediately", wide: true, reveal: "after",
    lines: [],
    visual: { kind: "code", lines: [
      ["202 Accepted", "accent"],
      ["{", "ink"],
      ["  \"name\": \"billing\",  \"status\": \"Pending\",", "muted"],
      ["  \"hostname\": \"billing-team-a.serverless.example\",", "muted"],
      ["  \"statusUrl\": \"/api/serverless/v1/groups/team-a/functions/billing\",", "muted"],
      ["  \"regions\": [{ \"region\": \"central\", \"status\": \"Pending\" },", "muted"],
      ["               { \"region\": \"south\",   \"status\": \"Pending\" }]", "muted"],
      ["}", "ink"],
      ["", "muted"],
      ["# validated synchronously — a 400 here means nothing was written", "accent"],
      ["# accepted, not finished: the work happens after the answer", "accent"],
    ] },
    notes: "Step two. The answer comes back before anything is running, and that is the point: validation is synchronous, so a 400 means nothing was written anywhere, while a 202 means the request was accepted and the work has started. What you get is the hostname the workload will answer on, a statusUrl to poll, and one row per region. Secrets sent in env or files are redacted in every response, never echoed." },
  // ---------------- 21 flow 3 of 3: the poll
  { kind: "content", kicker: "The API · 3 of 3 · the poll", title: "Then you poll the same URL", wide: true, reveal: "after",
    lines: [],
    visual: { kind: "code", lines: [
      ["GET /api/serverless/v1/groups/team-a/functions/billing", "accent"],
      ["", "muted"],
      ["200 OK", "accent"],
      ["{", "ink"],
      ["  \"name\": \"billing\",  \"status\": \"Ready\",", "muted"],
      ["  \"url\": \"https://billing-team-a.serverless.example\",", "muted"],
      ["  \"image\": \"registry.central/team-a/billing@sha256:9f2c...\",", "muted"],
      ["  \"scaling\": { \"minScale\": 0, \"maxScale\": 10 },", "muted"],
      ["  \"regions\": [", "muted"],
      ["    { \"region\": \"central\", \"status\": \"Ready\", \"revision\": \"billing-00007\" },", "muted"],
      ["    { \"region\": \"south\",   \"status\": \"Ready\", \"revision\": \"billing-00007\" }],", "muted"],
      ["  \"lastBuild\": { \"status\": \"Succeeded\", \"finishedAt\": \"...\" }", "muted"],
      ["}", "ink"],
      ["", "muted"],
      ["# Pending → Building → Deploying → Ready, same shape every time", "accent"],
    ] },
    notes: "Step three, and the loop closes. The statusUrl is an ordinary GET on the workload, and it answers with the same shape whatever the state: Pending, Building, Deploying or Ready, and Failed with a reason. Once it is Ready you get the URL to call, the image digest that was actually deployed, and the revision each region is serving. The portal polls exactly this and turns it into the tracker toast." },
  // ---------------- 22
  { kind: "content", kicker: "The API", title: "Talking to the cluster",
    lines: ["Client certificate mTLS, always", "No kubeconfig, no service-account token", "The certificate's CN is the user", "Address derived from the region name", "Every write is server-side apply"],
    visual: { kind: "graph", nodes: [
      N("cm", 30, 20, 240, 70, "cert-manager", "ACME, internal CA"),
      N("pod", 330, 20, 240, 70, "API pod", "mounts tls.crt + tls.key", "accent"),
      N("api", 330, 180, 240, 70, "API server", "api.{cluster}.{domain}:6443"),
      N("rbac", 30, 180, 240, 70, "RBAC", "role in the tenant namespace"),
      N("out", 130, 330, 340, 62, "workload applied", "server-side apply, with force"),
    ], edges: [E("cm", "pod"), E("pod", "api"), E("api", "rbac"), E("rbac", "out")] },
    notes: "One loop, in order: cert-manager issues an ACME client certificate, the API pod mounts it, the API server authenticates the certificate as a Kubernetes user, RBAC authorizes that user inside the tenant namespace, and the workload is applied. The CN is a DNS name because ACME only issues to DNS identities. The API server address is derived from the region's cluster name. Every write is a server-side apply with force, so retries heal partial state." },
  // ---------------- 23
  { kind: "content", kicker: "The API", title: "Active/active, two regions", reveal: "after",
    lines: ["The API runs in both regions", "One deploy fans out to both", "No leader election, deterministic specs", "A region builds what it runs", "State lives in the Knative Service"],
    visual: { kind: "graph", nodes: [
      N("dns", 150, 10, 300, 60, "DNS", "*.serverless.{domain} → active region", "accent"),
      N("c", 20, 150, 260, 120, "central", "API · controllers · registry · workloads"),
      N("s", 320, 150, 260, 120, "south", "API · controllers · registry · workloads"),
      N("ksvc", 120, 340, 360, 56, "Knative Service = the truth", "no database, no replication"),
    ], edges: [E("dns", "c"), E("dns", "s"), E("c", "s", { dashed: true }), E("c", "ksvc", { dashed: true }), E("s", "ksvc", { dashed: true })] },
    notes: "Make the points first, then bring the whole picture up at once. Two OpenShift clusters trusting the same CA. The API runs in both; DNS fronts the active one. A deploy fans out to both concurrently and rolls up per-region results. No leader election: specs contain no timestamps, UUIDs or counters, so two writers converge. Each region builds into its own registry. The Knative Service and its annotations are the replicated truth." },
  // ---------------- 24
  { kind: "content", kicker: "The API", title: "The tenant controller",
    lines: ["A namespace per SSO group", "Separate process, separate certificate", "The API never creates namespaces", "Provision before every deploy, fail closed", "Reconcile the local cluster only", "Garbage-collect empty namespaces, opt-in"],
    visual: { kind: "graph", nodes: [
      N("api", 20, 30, 220, 66, "API", "writes workloads only"),
      N("tc", 340, 30, 240, 66, "tenant controller", "namespaces, RBAC, policies", "accent"),
      N("nc", 20, 200, 260, 90, "central", "{group}-serverless"),
      N("ns", 320, 200, 260, 90, "south", "{group}-serverless"),
      N("tpl", 100, 340, 400, 56, "template set", "{{namespace}} {{group}} {{region}} {{registry}}"),
    ], edges: [E("api", "tc"), E("tc", "nc"), E("tc", "ns"), E("tpl", "tc", { dashed: true })] },
    notes: "Privilege separation is the reason: namespace and RBAC creation is cluster-scoped power the internet-facing API must not hold. Namespace per SSO group, identical in both clusters, rendered from region-neutral templates. Provision is called before every deploy and fails closed. Reconcile is local-cluster only so the regions never fight. The stamp protocol makes a converge crash-safe." },

  // ================ FUNCTIONS & BUILDS
  // ---------------- 25
  { kind: "section", title: "Functions & builds", sub: "'Here is my repo' hides a build." },
  // ---------------- 26
  { kind: "content", kicker: "Functions & builds", title: "Why a function needs a build", reveal: "paired",
    lines: ["A cluster cannot run a repository", "Kubernetes only ever starts images", "Someone must turn source into an image", "That step is the build", "For containers, the user already did it"],
    visual: { kind: "graph", nodes: [
      N("src", 20, 40, 180, 70, "your source", "a git repository"),
      N("gap", 240, 40, 140, 70, "?", "the missing step", "accent"),
      N("img", 420, 40, 160, 70, "an image", "what a pod runs"),
      N("fn", 190, 250, 200, 78, "function offering", "we build the image", "accent"),
      N("ctr", 410, 250, 180, 78, "container offering", "you bring the image"),
    ], edges: [E("src", "gap"), E("gap", "img"), E("fn", "gap", { dashed: true }), E("ctr", "img", { dashed: true })] },
    notes: "This slide is only about why the step exists. A cluster cannot run a git repository; a pod starts a container image and nothing else. So between source and a running workload there is always a step that produces an image. That step is the build. The container offering simply means the user has already done it and hands us the result; the function offering means we do it for them. How we do it comes next." },
  // ---------------- 27 buildpacks
  { kind: "content", kicker: "Functions & builds", title: "Buildpacks", reveal: "rows", logo: "buildpacks",
    lines: ["An open standard, not a Dockerfile", "Detection reads your source", "The same base image for everyone", "Rebase swaps that base, no rebuild", "A CNCF project, like Knative"],
    visual: { kind: "table", head: ["", "Dockerfile", "Buildpacks"], rows: [
      ["Who writes it", "Every team, again", "Nobody"],
      ["Base image", "Whatever was pinned", "One curated stack"],
      ["Language setup", "Copied between repos", "Detected from source"],
      ["Patching the base", "Rebuild and hope", "Rebase, in seconds"],
      ["Consistency", "Per repository", "Platform-wide"],
    ] },
    notes: "Cloud Native Buildpacks are a CNCF specification, not something we invented. Instead of a Dockerfile per repository, a detection phase inspects the source: requirements.txt or pyproject means Python, go.mod means Go, package.json means Node. Every image comes off the same curated base, and because that base is a separate layer it can be swapped by rebase without rerunning the build." },
  // ---------------- 28 paketo
  { kind: "content", kicker: "Functions & builds", title: "Paketo: the buildpacks we run", reveal: "rows",
    lines: ["The open-source implementation we use", "One buildpack per language, plus shared ones", "Detection picks the group for you", "We mirror them into the airgap", "Python, Go and Node today"],
    visual: { kind: "table", plain: true, head: ["Buildpack", "Detects", "Gives you"], rows: [
      ["paketo/python", "requirements.txt", "CPython, pip install"],
      ["paketo/go", "go.mod", "a compiled binary"],
      ["paketo/nodejs", "package.json", "Node, npm ci"],
      ["paketo/ca-certificates", "always", "our internal CA"],
      ["paketo/procfile", "Procfile", "the start command"],
    ] },
    notes: "Buildpacks are a specification; Paketo is the open-source family that implements it, and it is what our ClusterStore holds. Detection runs them in order and keeps the group that matches, so a repository with a go.mod gets the Go buildpack and nothing else. The ca-certificates buildpack is the one that matters most for us: it is how the internal CA ends up in the image. The mirror scripts pull the buildpackages and the runtimes they fetch at build time." },
  // ---------------- 29 kpack, no diagram
  { kind: "content", kicker: "Functions & builds", title: "kpack: buildpacks on Kubernetes", reveal: "paired",
    lines: ["Buildpacks need something to run them", "kpack runs them as pods, in-cluster", "You declare an Image, it builds", "It rebuilds on commit or base change", "The output is a digest, nothing else"],
    visual: { kind: "stack", layers: [
      ["kpack", "a Kubernetes controller", "accent"],
      ["declarative", "you write an Image, not a pipeline"],
      ["in-cluster", "one pod per build"],
      ["rebuilds", "new commit · new base · new buildpack"],
      ["output", "a registry digest"],
    ] },
    notes: "kpack is the Kubernetes controller that runs buildpacks. It is declarative in the way the rest of Kubernetes is: you do not trigger a pipeline, you declare that an image should exist for a repository and a builder, and the controller keeps that true. It rebuilds when the commit changes, when the base image is patched, or when a buildpack is updated. What it produces is a digest in a registry, and nothing else. The objects are on the next slide." },
  // ---------------- 30 all kpack resources, click to explain
  { kind: "content", kicker: "Functions & builds", title: "The kpack resources", wide: true, clickYaml: true, reveal: "rows",
    hint: "click any resource for what it does",
    lines: [],
    visual: { kind: "graph", box: [1030, 395], nodes: [
      N("cstack", 20, 0, 200, 62, "ClusterStack", "build + run base", "teal", null,
        "Cluster-scoped. The pair of base images every build uses: one to build on, one to run on. Patching the stack is what makes a rebase possible."),
      N("cstore", 20, 85, 200, 62, "ClusterStore", "the buildpacks", "teal", null,
        "Cluster-scoped. The catalogue of buildpackages available to builders — for us, the mirrored Paketo family."),
      N("cbuilder", 300, 42, 220, 66, "ClusterBuilder", "stack + store, baked", "accent", null,
        "Cluster-scoped. Combines a ClusterStack and a ClusterStore into one builder image that builds actually run. Rebuilt when either changes."),
      N("image", 300, 190, 200, 66, "Image", "what to build", "accent", null,
        "Namespaced, and the only object we write. It names the source, the builder, the tag to push and the credentials. The controller keeps it true."),
      N("sr", 20, 190, 200, 66, "SourceResolver", "branch → commit SHA", "teal", null,
        "Created by the Image. It watches the git ref and resolves it to a concrete commit, which is what triggers a rebuild when someone pushes."),
      N("creds", 300, 320, 200, 62, "Secret + SA", "git and registry", "teal", null,
        "The git Secret clones the repository, the build ServiceAccount pushes to the region registry. The API creates both next to the Image."),
      N("build", 580, 190, 200, 66, "Build", "one attempt", "teal", null,
        "One numbered attempt at one commit with one builder. Immutable: a retry is a new Build, so the history of what was tried stays readable."),
      N("pod", 830, 190, 180, 66, "Pod", "the lifecycle runs", "teal", null,
        "Each Build becomes a pod whose init containers are the buildpack lifecycle phases. That is why every phase has its own log stream."),
    ], edges: [
      E("cstack", "cbuilder"), E("cstore", "cbuilder"), E("cbuilder", "image", { dashed: true }),
      E("image", "sr"), E("creds", "image", { dashed: true }), E("image", "build"), E("build", "pod"),
    ] },
    notes: "Every object kpack has, and how they relate. The top row is cluster-scoped and shared: a ClusterStack is the base image pair, a ClusterStore is the buildpack catalogue, and a ClusterBuilder bakes the two together into the image builds actually run. Below that is the namespaced part, and the Image is the only object we ever write. It creates a SourceResolver to turn a branch into a commit, and a Build per attempt, and each Build is a pod running the lifecycle. In the web deck, click any resource for what it does." },
  // ---------------- 31 lifecycle phases, after the text
  { kind: "content", kicker: "Functions & builds", title: "A build, phase by phase", reveal: "rows",
    lines: ["The lifecycle runs as init containers", "Each phase is a named container", "Each phase has its own log", "Cached layers make a rerun fast", "Export assembles and pushes the image"],
    visual: { kind: "phases", items: [["prepare", "fetch the source"], ["analyze", "read the previous image"], ["detect", "pick the buildpacks"], ["restore", "bring back cached layers"], ["build", "install and compile"], ["export", "assemble and push"], ["completion", "finish up"]] },
    notes: "This is the Cloud Native Buildpacks lifecycle, the same everywhere kpack runs, with nothing of ours in it. Make the five points first, then bring the phases up one per click. It executes as ordered init containers on a single pod, which is why each phase has its own log stream and you can point at exactly where a build failed. Detect chooses the buildpack group, restore pulls cached layers so a rerun is fast, and export is the phase that assembles the OCI layers and pushes them." },
  // ---------------- 32 our chart
  { kind: "content", kicker: "Functions & builds", title: "Our kpack chart", reveal: "rows",
    lines: ["Not a fork, a Helm chart", "Packages upstream kpack faithfully", "CRDs as templates, for the webhook", "Creates the cluster build content", "Mirror scripts for the airgap"],
    visual: { kind: "stack", layers: [
      ["the chart", "upstream kpack 0.18", "accent"],
      ["CRDs", "templated, not an untouched crds/"],
      ["clusterBuild", "ClusterStack + ClusterStore"],
      ["credentials", "projected by external-secrets"],
      ["scripts/mirror", "images, buildpackages, runtimes"],
    ] },
    notes: "Our kpack repository is not a fork of upstream; it is a Helm chart that packages upstream kpack 0.18 and adds what an airgap needs. The CRDs ship as templates rather than an untouched crds/ directory because the conversion webhook's clientConfig has to be templated to the release namespace. The clusterBuild block creates the ClusterStack and ClusterStore plus the credentials that pull them. The mirror scripts pull three classes of artifact, including the runtimes that buildpacks fetch at build time, which nothing else mirrors." },
  // ---------------- 33 how the API uses kpack
  { kind: "content", kicker: "Functions & builds", title: "How the API uses kpack", reveal: "paired",
    lines: ["The API declares, it never builds", "One Image per function, per region", "A git Secret for the clone", "A build ServiceAccount for the registry", "Owned by the Knative Service"],
    visual: { kind: "graph", nodes: [
      N("api", 190, 10, 220, 60, "API", "writes manifests only", "accent"),
      N("img", 10, 150, 180, 64, "kpack Image", "named for the function"),
      N("sec", 210, 150, 180, 64, "git Secret", "clone credentials"),
      N("sa", 410, 150, 180, 64, "build SA", "registry credentials"),
      N("reg", 190, 300, 220, 64, "region registry", "{group}/{name}:{branch}"),
    ], edges: [E("api", "img"), E("api", "sec"), E("api", "sa"), E("img", "reg")] },
    notes: "Now our side. The API is pure declaration: it emits a kpack Image, a git Secret and a build ServiceAccount into the workload's own tenant namespace, in the same pass as the Knative Service, and stamps them with the Knative Service's ownerReference so deleting the function garbage-collects the build objects. Everything is per region, and the image is pushed to that region's registry under the group and name. A Kyverno policy injects the internal CA into the build pod, initContainers included, because prepare is where the clone happens." },
  // ---------------- 34
  { kind: "content", kicker: "Functions & builds", title: "The build controller", reveal: "paired",
    lines: ["Watches Image.status.latestImage", "Applies the digest onto the Service", "Both ends stay in one region", "Only ever writes digests", "Ships with no web stack"],
    visual: { kind: "graph", nodes: [
      N("im", 20, 60, 160, 70, "kpack Image", "latestImage changes"),
      N("bc", 220, 60, 160, 70, "build controller", "watch", "accent"),
      N("ks", 420, 60, 160, 70, "Knative Service", "image @sha256"),
      N("reg", 20, 240, 160, 70, "region registry", "the digest lives here"),
      N("rev", 420, 240, 160, 70, "new Revision", "Knative rolls it out"),
    ], edges: [E("im", "bc"), E("bc", "ks"), E("reg", "im", { dashed: true }), E("ks", "rev")] },
    notes: "The API declares a build and walks away. The build controller watches kpack Images in its own region and, when latestImage changes, server-side applies the live Knative Service with the digest. Exactly one writer per phase, and the controller only ever writes digests. It ships without a web stack, and CI proves it by importing the service out of the image and failing if fastapi or jwt are present." },

  // ================ PORTAL & THE FUTURE
  // ---------------- 35
  { kind: "section", title: "Portal & the future", sub: "A console, and a library for the next API." },
  // ---------------- 36
  { kind: "content", kicker: "Portal & the future", title: "How the portal wires it",
    lines: ["Next.js 16, server components", "Keycloak token stays server-side", "The SSO group is the project", "Forms are built from /info", "Live logs over SSE tickets", "Airgap-native: no CDN, no fonts"],
    visual: { kind: "graph", nodes: [
      N("br", 20, 60, 150, 70, "Browser", "React 19"),
      N("po", 225, 60, 150, 70, "Portal", "server actions", "accent"),
      N("ap", 430, 60, 150, 70, "Serverless API", "/api/serverless/v1"),
      N("tk", 20, 230, 150, 70, "?ticket=", "60 s, one path"),
      N("kc", 225, 230, 150, 70, "Keycloak", "OIDC, groups claim"),
    ], edges: [E("br", "po"), E("po", "ap"), E("po", "kc", { dashed: true }), E("tk", "ap", { dashed: true }), E("br", "tk", { dashed: true })] },
    notes: "Next.js 16 with server components and server actions in front of the API. The user's Keycloak token stays in the encrypted session and is forwarded server-side. The SSO group is the project. Forms are built from /info; the create flow turns 202 into a tracker toast. Live logs and stats stream over SSE using re-minted tickets." },
  // ---------------- 37
  { kind: "content", kicker: "Portal & the future", title: "A console, not a Serverless UI",
    lines: ["Serverless is live, Compute is next", "An offering is data, not code", "The shell is already generic", "Next: shared layer, generated client"],
    visual: { kind: "tiles", items: ["Compute", "Serverless", "Databases", "Networking", "Object Storage", "Observability", "Data Integration", "Data Analytics", "Dev Tools", "Machine Learning", "Security", "…as data"], live: "Serverless", next: ["Compute", "Dev Tools"] },
    notes: "The catalog already names ten future offerings; Serverless is live, and Compute and Dev Tools are the two we expect next. Adding an offering is data, not code: a JSON entry or one environment variable lights up a card, a route segment and the nav. The shell is generic; the serverless tree is the only product-specific part. Next: a shared resource layer and a generated TypeScript client so the contract stops being copied by hand." },
  // ---------------- 38
  { kind: "content", kicker: "Portal & the future", title: "cloudlet-apis: start at step five", reveal: "rows",
    lines: ["Extracted from the Serverless API", "core, web and auth extras", "Layering enforced by tests", "A new API wires in five steps", "Same group rules everywhere"],
    visual: { kind: "stack", layers: [
      ["[auth]", "OIDC · admin key · token proxy · tickets", "accent"],
      ["[web]", "health · error envelope · offline docs"],
      ["core", "errors · names · logging · request id"],
    ] },
    notes: "cloudlet-apis is the shared Python library extracted from the Serverless API: core, web and auth as install extras, with layering enforced by tests. A new API wires it in five steps and inherits the security review. Two APIs normalizing groups differently is an authorization bug. Next extraction candidates: the mTLS multi-cluster client and the region fan-out." },
  // ---------------- 39
  { kind: "closing", kicker: "What we built", title: "Code in, a running service out.",
    stats: [["4", "repositories"], ["3", "services"], ["23", "endpoints"], ["2", "regions"], ["3", "runtimes"], ["0", "kubectl"]],
    lines: ["Operator owns Knative; we own the contract", "Deterministic specs, so no leader", "Builds declared, watched, rolled in", "The second product is cheaper"],
    notes: "Close on the four takeaways and open for questions." },
];

slides[1].visual.text = String(slides.length);
module.exports = { slides };
