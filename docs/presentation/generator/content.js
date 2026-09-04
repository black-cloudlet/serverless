// One content model for both renderers. 7x7 rule: <= 7 lines per slide, <= 7 words per line.
// Visual coordinates live in a 600 x 440 box.
//
// reveal: how the visual animates.
//   "auto"   (default) the whole visual builds itself when the slide opens
//   "paired" visual element k appears together with line k
//   "rows"   visual elements appear one per click, after all the lines
//   "after"  the whole visual appears on one click, after all the lines

const N = (id, x, y, w, h, label, sub, tone, yaml) => ({ id, x, y, w, h, label, sub, tone: tone || "teal", yaml });
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
  // ---------------- 3  (new: definition)
  { kind: "content", kicker: "Foundations", title: "Serverless, defined",
    lines: ["The servers exist, you stop managing them", "You hand over a unit of code", "The platform decides where it runs", "It runs only while work arrives", "You are billed for that time"],
    visual: { kind: "glyph", text: "serverless ≠ no servers", sub: "someone else's problem, on purpose" },
    notes: "Definition first, before any properties. Serverless does not mean there is no server; it means the team shipping the code does not own the machine, the capacity, or the scaling. The unit you hand over is code or an image, not a host. The platform places it, starts it on demand and stops it when idle, and the bill follows the work rather than the clock." },
  // ---------------- 4  (properties)
  { kind: "content", kicker: "Foundations", title: "What that buys you",
    lines: ["Scale to zero when idle", "Scale out when traffic arrives", "Pay for requests, not hours", "Nothing to patch, nothing to plan", "One deploy, no capacity meeting"],
    visual: { kind: "glyph", text: "0 → N → 0", sub: "replicas follow traffic" },
    notes: "These are the consequences of the definition. Scale to zero is the defining property: an idle workload costs nothing and needs nobody. Scaling out is automatic. Patching the base and the runtime is the platform's job, so nobody plans capacity for a function." },
  // ---------------- 5  (trade-offs, rows animate)
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
  // ---------------- 6  (new: Knative definition and history)
  { kind: "content", kicker: "Foundations", title: "Knative, and where it came from",
    lines: ["Google open-sourced it in 2018", "A CNCF project since 2022", "Serverless building blocks for Kubernetes", "Two halves: Serving and Eventing", "We run Serving only"],
    visual: { kind: "stack", layers: [
      ["2018", "Google open-sources Knative", "accent"],
      ["2022", "donated to the CNCF"],
      ["Serving", "request-driven workloads"],
      ["Eventing", "brokers, triggers, CloudEvents"],
      ["Ours", "Serving, via the Operator"],
    ] },
    notes: "Knative was opened up by Google in 2018 and became a CNCF incubating project in 2022, so it is not a vendor-only technology. It is not a platform in itself: it is a set of Kubernetes building blocks. Serving covers request-driven workloads, Eventing covers event routing with brokers and triggers. We run Serving; Eventing exists but we do not expose it." },
  // ---------------- 7  (from what we already run; table after the points)
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
  // ---------------- 8  (the object; each block with its line; YAML on click)
  { kind: "content", kicker: "Foundations", title: "One object: the Knative Service", reveal: "paired", clickYaml: true,
    lines: ["The Service owns Configuration and Route", "Configuration holds the desired code", "Every change is an immutable Revision", "Route sends traffic to revisions", "DomainMapping gives it your hostname"],
    visual: { kind: "graph", nodes: [
      N("svc", 170, 10, 260, 64, "Knative Service", "the one object you write", "accent",
        "apiVersion: serving.knative.dev/v1\nkind: Service\nmetadata:\n  name: billing\n  namespace: team-a-serverless\nspec:\n  template:\n    spec:\n      containers:\n        - image: registry/billing@sha256:..."),
      N("cfg", 20, 140, 240, 64, "Configuration", "desired state of the code", "teal",
        "kind: Configuration\nspec:\n  template:\n    metadata:\n      annotations:\n        autoscaling.knative.dev/min-scale: \"0\"\n        autoscaling.knative.dev/max-scale: \"10\"\n    spec:\n      containers:\n        - image: registry/billing@sha256:..."),
      N("rev", 20, 270, 240, 64, "Revision N", "immutable snapshot", "teal",
        "kind: Revision\nmetadata:\n  name: billing-00007\nspec:\n  containers:\n    - image: registry/billing@sha256:...\nstatus:\n  conditions:\n    - type: Ready\n      status: \"True\""),
      N("rt", 340, 140, 240, 64, "Route", "traffic → revisions", "teal",
        "kind: Route\nspec:\n  traffic:\n    - revisionName: billing-00007\n      percent: 100\nstatus:\n  url: http://billing.team-a-serverless.svc"),
      N("dm", 340, 270, 240, 64, "DomainMapping", "your own hostname", "teal",
        "apiVersion: serving.knative.dev/v1beta1\nkind: DomainMapping\nmetadata:\n  name: billing-team-a.serverless.example\nspec:\n  ref:\n    kind: Service\n    name: billing"),
    ], edges: [E("svc", "cfg"), E("cfg", "rev"), E("svc", "rt"), E("rt", "rev", { dashed: true }), E("dm", "rt", { dashed: true })] },
    notes: "Each block lands with the line that explains it. A Service owns a Configuration and a Route. Every change to the Configuration stamps a new immutable Revision; old ones stay addressable. The Route decides which revisions receive traffic and in what split. A DomainMapping attaches a hostname of your choosing, which is how we give one workload the same address in both regions. In the web deck, click any block to see the YAML for that resource." },
  // ---------------- 9  (new: the pieces, before the flow)
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
  // ---------------- 10 (the flow; block with its line)
  { kind: "content", kicker: "Foundations", title: "Scaling to zero, and back", reveal: "paired",
    lines: ["An idle revision drops to zero pods", "The next request reaches the Activator", "The Activator asks the KPA for capacity", "Pods start and the request continues", "Once healthy, traffic goes direct"],
    visual: { kind: "graph", nodes: [
      N("pods0", 20, 30, 190, 64, "Revision at zero", "no pods, no cost"),
      N("act", 260, 30, 200, 64, "Activator", "buffers the request", "accent"),
      N("kpa", 20, 190, 190, 64, "KPA", "asks for replicas"),
      N("pods", 260, 190, 200, 64, "Revision pods", "0 → N"),
      N("ready", 260, 330, 200, 64, "Ready", "Activator steps out"),
    ], edges: [E("pods0", "act"), E("act", "kpa"), E("kpa", "pods"), E("pods", "ready")] },
    notes: "Now the flow, one box per line. When nothing arrives the revision drops to zero pods and the Activator takes its place in the data path. The next request is held by the Activator, which asks the KPA for capacity; pods start, the request is forwarded, and once the revision is healthy the Activator steps out of the path and traffic goes straight to the pods." },
  // ---------------- 11
  { kind: "content", kicker: "Foundations", title: "The OpenShift Serverless Operator",
    lines: ["Red Hat's supported Knative distribution", "One KnativeServing custom resource", "OLM installs it and upgrades it", "Kourier ingress, Routes created for you", "Catalog mirrors with oc-mirror"],
    visual: { kind: "stack", layers: [["Knative Serving", "Service · Revision · Route · DomainMapping", "accent"], ["KnativeServing CR", "one document"], ["Serverless Operator", "reconciles, upgrades"], ["Operator Lifecycle Manager", "Subscription → catalog"], ["OpenShift", "one cluster per region"]] },
    notes: "The operator is Red Hat's packaging of Knative. One KnativeServing CR, reconciled by the operator, upgraded through OLM. Kourier is the ingress; Routes are created for us. The catalog mirrors with oc-mirror, which is what makes it viable in an airgap." },
  // ---------------- 12
  { kind: "content", kicker: "Foundations", title: "Why the Operator",
    lines: ["Every alternative made us own more", "We never create a Route ourselves", "Airgap needs one supported mirror path"],
    visual: { kind: "table", head: ["", "Upstream Knative", "Serverless Operator"], rows: [
      ["Upgrades", "YAML we maintain", "OLM, from the catalog"],
      ["Ingress", "Run it ourselves", "Kourier, managed"],
      ["Routes, TLS", "Hand-made, RBAC", "Created for us"],
      ["Airgap", "Image by image", "oc-mirror, once"],
      ["Support", "Community", "Red Hat"],
    ] },
    notes: "The decision is about ownership. Upstream would mean maintaining the install, the ingress, the Routes and the mirror ourselves, with community support. The chart assumes the operator's conventions and the API holds no routes RBAC. Recorded in docs/DEPLOYING.md and the locked decisions in docs/ARCHITECTURE.md." },

  // ================ THE API
  // ---------------- 13
  { kind: "section", title: "The API", sub: "One HTTP call, a serverless application." },
  // ---------------- 14
  { kind: "content", kicker: "The API", title: "One call, every cluster",
    lines: ["Functions from git, containers from images", "Validate now, answer 202, poll status", "Closed status vocabulary, published on /info", "FastAPI, Pydantic, Kubernetes-client, cloudlet-apis", "One Helm chart, rendered per region"],
    visual: { kind: "stats", items: [["1", "call to deploy everywhere"], ["23", "endpoints"], ["3", "runtimes"], ["0", "kubectl for users"]] },
    notes: "The API is a FastAPI control plane. Every write validates synchronously then returns 202 with a statusUrl; the status vocabulary is closed and published on /info so no client hardcodes it. One Helm chart, rendered by ArgoCD once per region." },
  // ---------------- 15 (containers expanded like functions)
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
  // ---------------- 16
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
  // ---------------- 17 (new: statuses, animated)
  { kind: "content", kicker: "The API", title: "Every workload has one status", reveal: "rows",
    lines: ["A closed set, published on /info", "Pending, Building, Deploying, Ready", "Building only exists for functions", "Failed always carries a reason", "One row per region, rolled up"],
    visual: { kind: "lifecycle", phases: ["Pending", "Building", "Deploying", "Ready"], failed: ["BuildFailed", "ImagePullFailed", "CrashLooping", "ConfigError", "ProgressDeadlineExceeded"] },
    notes: "The status vocabulary is closed and published on /info so no client hardcodes it. Walk the happy path one chip at a time, then the failure path. Building only appears for functions, because only a function has a build. Terminating follows a delete. Failed always carries a machine-readable reason next to the human message, and every region reports its own status which the API rolls up into one." },
  // ---------------- 18 (new: JSON request and response)
  { kind: "content", kicker: "The API", title: "A request, and what comes back", wide: true, reveal: "after",
    lines: [],
    visual: { kind: "code", lines: [
      ["POST /api/serverless/v1/groups/team-a/functions", "accent"],
      ["{", "ink"],
      ["  \"name\": \"billing\",  \"port\": 8080,  \"size\": \"small\",", "muted"],
      ["  \"gitRepo\": \"https://git.internal/team-a/billing.git\",", "muted"],
      ["  \"branch\": \"main\",  \"runtime\": \"python\",  \"version\": \"3.12\",", "muted"],
      ["  \"regions\": [\"central\", \"south\"],", "muted"],
      ["  \"env\": [{ \"name\": \"LOG_LEVEL\", \"value\": \"info\" }]", "muted"],
      ["}", "ink"],
      ["", "muted"],
      ["202 Accepted", "accent"],
      ["{", "ink"],
      ["  \"name\": \"billing\",  \"status\": \"Pending\",", "muted"],
      ["  \"hostname\": \"billing-team-a.serverless.example\",", "muted"],
      ["  \"statusUrl\": \"/groups/team-a/functions/billing\",", "muted"],
      ["  \"regions\": [{ \"region\": \"central\", \"status\": \"Pending\" },", "muted"],
      ["               { \"region\": \"south\",   \"status\": \"Pending\" }]", "muted"],
      ["}", "ink"],
    ] },
    notes: "A real create, and the 202 that answers it. Note what the caller does not send: no namespace, no image, no Knative anything. Note what comes back immediately: a status of Pending, the hostname the workload will answer on, a statusUrl to poll, and one row per region. Secrets sent in env or files come back redacted, never echoed." },
  // ---------------- 19 (redrawn, CN box removed)
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
  // ---------------- 20 (diagram after the text)
  { kind: "content", kicker: "The API", title: "Active/active, two regions", reveal: "after",
    lines: ["The API runs in both regions", "One deploy fans out to both", "No leader election, deterministic specs", "A region builds what it runs", "State lives in the Knative Service"],
    visual: { kind: "graph", nodes: [
      N("dns", 150, 10, 300, 60, "DNS", "*.serverless.{domain} → active region", "accent"),
      N("c", 20, 150, 260, 120, "central", "API · controllers · registry · workloads"),
      N("s", 320, 150, 260, 120, "south", "API · controllers · registry · workloads"),
      N("ksvc", 120, 340, 360, 56, "Knative Service = the truth", "no database, no replication"),
    ], edges: [E("dns", "c"), E("dns", "s"), E("c", "s", { dashed: true }), E("c", "ksvc", { dashed: true }), E("s", "ksvc", { dashed: true })] },
    notes: "Make the points first, then bring the whole picture up at once. Two OpenShift clusters trusting the same CA. The API runs in both; DNS fronts the active one. A deploy fans out to both concurrently and rolls up per-region results. No leader election: specs contain no timestamps, UUIDs or counters, so two writers converge. Each region builds into its own registry. The Knative Service and its annotations are the replicated truth." },
  // ---------------- 21
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
  // ---------------- 22
  { kind: "section", title: "Functions & builds", sub: "'Here is my repo' hides a build." },
  // ---------------- 23 (rewritten: why a build is needed at all)
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
  // ---------------- 24 (new: buildpacks, generic)
  { kind: "content", kicker: "Functions & builds", title: "Buildpacks", reveal: "rows",
    lines: ["An open standard, not a Dockerfile", "Detection reads your source", "The same base image for everyone", "Rebase swaps that base, no rebuild", "Paketo is the family we mirror"],
    visual: { kind: "table", head: ["", "Dockerfile", "Buildpacks"], rows: [
      ["Who writes it", "Every team, again", "Nobody"],
      ["Base image", "Whatever was pinned", "One curated stack"],
      ["Language setup", "Copied between repos", "Detected from source"],
      ["Patching the base", "Rebuild and hope", "Rebase, in seconds"],
      ["Consistency", "Per repository", "Platform-wide"],
    ] },
    notes: "Cloud Native Buildpacks are a CNCF specification, not something we invented. Instead of a Dockerfile per repository, a detection phase inspects the source: requirements.txt or pyproject means Python, go.mod means Go, package.json means Node. Every image comes off the same curated base, and because that base is a separate layer it can be swapped by rebase without rerunning the build. Paketo is the buildpack family we mirror." },
  // ---------------- 25 (new: kpack, generic)
  { kind: "content", kicker: "Functions & builds", title: "kpack: buildpacks on Kubernetes", reveal: "paired",
    lines: ["Buildpacks need something to run them", "kpack runs them as pods", "Image says what to build", "Build is a single attempt", "Builder holds stack and buildpacks"],
    visual: { kind: "graph", nodes: [
      N("image", 190, 10, 220, 64, "Image", "the desired build", "accent"),
      N("sr", 20, 150, 230, 64, "SourceResolver", "git ref → commit SHA"),
      N("build", 350, 150, 230, 64, "Build", "one attempt, numbered"),
      N("pod", 350, 300, 230, 64, "Pod", "the lifecycle runs here"),
      N("builder", 20, 300, 230, 64, "Builder", "stack + buildpacks"),
    ], edges: [E("image", "sr"), E("image", "build"), E("build", "pod"), E("builder", "build", { dashed: true })] },
    notes: "kpack is the Kubernetes controller that runs buildpacks, and its objects arrive one per line. An Image is the declaration of what should be built and from where. A SourceResolver turns a branch into a concrete commit. A Build is one attempt at that commit, numbered. Each Build ends up as a Pod. A Builder is the toolchain, the stack plus the buildpacks, that the build uses. Nothing here is specific to us yet." },
  // ---------------- 26 (new: generic lifecycle phases)
  { kind: "content", kicker: "Functions & builds", title: "A build, phase by phase",
    lines: ["The lifecycle runs as init containers", "Each phase is a named container", "Each phase has its own log", "Cached layers make a rerun fast", "Export assembles and pushes the image"],
    visual: { kind: "phases", items: [["prepare", "fetch the source"], ["analyze", "read the previous image"], ["detect", "pick the buildpacks"], ["restore", "bring back cached layers"], ["build", "install and compile"], ["export", "assemble and push"], ["completion", "finish up"]] },
    notes: "This is the Cloud Native Buildpacks lifecycle, the same everywhere kpack runs, with nothing of ours in it. It executes as ordered init containers on a single pod, which is why each phase has its own log stream and you can point at exactly where a build failed. Detect chooses the buildpack group, restore pulls cached layers so a rerun is fast, and export is the phase that assembles the OCI layers and pushes them." },
  // ---------------- 27 (new: our chart)
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
  // ---------------- 28 (new: how the API uses kpack)
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
  // ---------------- 29
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
  // ---------------- 30
  { kind: "section", title: "Portal & the future", sub: "A console, and a library for the next API." },
  // ---------------- 31
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
  // ---------------- 32 (Compute and Dev Tools are next)
  { kind: "content", kicker: "Portal & the future", title: "A console, not a Serverless UI",
    lines: ["Serverless is live, Compute is next", "An offering is data, not code", "The shell is already generic", "Next: shared layer, generated client"],
    visual: { kind: "tiles", items: ["Compute", "Serverless", "Databases", "Networking", "Object Storage", "Observability", "Data Integration", "Data Analytics", "Dev Tools", "Machine Learning", "Security", "…as data"], live: "Serverless", next: ["Compute", "Dev Tools"] },
    notes: "The catalog already names ten future offerings; Serverless is live, and Compute and Dev Tools are the two we expect next. Adding an offering is data, not code: a JSON entry or one environment variable lights up a card, a route segment and the nav. The shell is generic; the serverless tree is the only product-specific part. Next: a shared resource layer and a generated TypeScript client so the contract stops being copied by hand." },
  // ---------------- 33
  { kind: "content", kicker: "Portal & the future", title: "cloudlet-apis: start at step five", reveal: "rows",
    lines: ["Extracted from the Serverless API", "core, web and auth extras", "Layering enforced by tests", "A new API wires in five steps", "Same group rules everywhere"],
    visual: { kind: "stack", layers: [
      ["[auth]", "OIDC · admin key · token proxy · tickets", "accent"],
      ["[web]", "health · error envelope · offline docs"],
      ["core", "errors · names · logging · request id"],
    ] },
    notes: "cloudlet-apis is the shared Python library extracted from the Serverless API: core, web and auth as install extras, with layering enforced by tests. A new API wires it in five steps and inherits the security review. Two APIs normalizing groups differently is an authorization bug. Next extraction candidates: the mTLS multi-cluster client and the region fan-out." },
  // ---------------- 34
  { kind: "closing", kicker: "What we built", title: "Code in, a running service out.",
    stats: [["4", "repositories"], ["3", "services"], ["23", "endpoints"], ["2", "regions"], ["3", "runtimes"], ["0", "kubectl"]],
    lines: ["Operator owns Knative; we own the contract", "Deterministic specs, so no leader", "Builds declared, watched, rolled in", "The second product is cheaper"],
    notes: "Close on the four takeaways and open for questions." },
];

slides[1].visual.text = String(slides.length);
module.exports = { slides };
