// One content model for both renderers. 7x7 rule: <= 7 lines per slide, <= 7 words per line.
// Visual coordinates live in a 600 x 440 box.

const N = (id, x, y, w, h, label, sub, tone) => ({ id, x, y, w, h, label, sub, tone: tone || "teal" });
const E = (from, to, opts) => ({ from, to, ...(opts || {}) });

const slides = [
  // ---------------- 1
  { kind: "title", kicker: "Cloudlet's Serverless", title: "From Knative to the Portal", sub: "What we built, and why.", meta: "Team review · September 2026",
    notes: "Open with the one-sentence promise: a developer gives us code or an image and gets a running, addressable service in both regions without ever seeing Kubernetes." },
  // ---------------- 2
  { kind: "content", chapter: 0, kicker: "Today", title: "What we'll talk about",
    lines: ["What is serverless computing", "What is Knative and OpenShift Serverless Operator", "The API: active-active Knative wrapper", "Functions: buildpacks, kpack, build controller", "The portal and cloudlet-apis", "Where we go next"],
    visual: { kind: "glyph", text: "SLIDES", sub: "slides · six topics · one platform" },
    notes: "Foundations is the vocabulary. The API is the contract. Functions and builds is where most of the engineering went. Portal and future is what the platform becomes next." },
  // ---------------- 3
  { kind: "content", chapter: 1, kicker: "Foundations", title: "What is serverless?",
    lines: ["You bring code, not servers", "Scale to zero when idle", "Scale out when traffic arrives", "Pay for requests, not hours", "Nothing to patch, nothing to plan"],
    visual: { kind: "glyph", text: "0 → N → 0", sub: "replicas follow traffic" },
    notes: "Serverless is a billing and operations model, not a technology. Scale to zero is the defining property: an idle workload costs nothing and needs nobody." },
  // ---------------- 3b: pros and cons
  { kind: "content", chapter: 1, kicker: "Foundations", title: "Serverless: the trade-offs",
    lines: ["Cheap when idle, pricey at peak", "Zero ops, but less control", "Elastic scale, but cold starts", "Fast to ship, easy to lock in", "Fits bursty traffic, not long jobs"],
    visual: { kind: "table", plain: true, head: ["", "Pro", "Con"], rows: [
      ["Cost", "Pay only when it runs", "Bills spike under load"],
      ["Ops", "No servers to patch", "Runtime is not yours"],
      ["Scale", "Up and down by itself", "Cold start on first hit"],
      ["Speed", "A function in minutes", "Platform lock-in"],
      ["Fit", "Bursty, event-driven", "Long-running jobs, state"],
    ] },
    notes: "Be honest about the trade-offs before the architecture. Cost: nothing when idle, but a hot loop of requests is billed per request. Ops: no patching, but you cannot tune the kernel or the runtime. Scale: automatic in both directions, but the first request after idle pays a cold start. Speed: a function ships in minutes, but the deployment model is the platform's. Fit: request-driven and bursty workloads win; long-running or stateful jobs do not belong here." },
  // ---------------- 4a: Knative from familiar objects
  { kind: "content", chapter: 1, kicker: "Foundations", title: "Knative, from what we already run",
    lines: ["We know Deployment, Service, Ingress, HPA", "Knative bundles them into one object", "Adds revisions, routing and scale-to-zero", "Runs on any Kubernetes, ours is OpenShift"],
    visual: { kind: "table", head: ["Need", "Plain Kubernetes", "Knative"], rows: [
      ["Run the code", "Deployment", "Revision"],
      ["Reach it", "Service + Ingress", "Route"],
      ["Scale it", "HPA, never to zero", "KPA, down to zero"],
      ["Change it", "Rolling update", "New Revision, old kept"],
      ["Own hostname", "Ingress rule", "DomainMapping"],
    ] },
    notes: "Start from the objects the team writes every day. A Deployment runs the code, a Service plus an Ingress reaches it, an HPA scales it but never below one, a rolling update replaces it. Knative Serving covers the same needs with one object and adds what plain Kubernetes lacks: immutable revisions you can route between, and scaling to zero." },
  // ---------------- 4b: the Knative Service object
  { kind: "content", chapter: 1, kicker: "Foundations", title: "One object: the Knative Service",
    lines: ["The Service owns Configuration and Route", "Configuration holds the desired code", "Every change is an immutable Revision", "Route sends traffic to revisions", "DomainMapping gives it your hostname"],
    visual: { kind: "graph", nodes: [
      N("svc", 170, 10, 260, 64, "Knative Service", "the one object you write", "accent"),
      N("cfg", 20, 140, 240, 64, "Configuration", "desired state of the code"),
      N("rt", 340, 140, 240, 64, "Route", "traffic → revisions"),
      N("rev", 20, 270, 240, 64, "Revision N", "immutable snapshot"),
      N("dm", 340, 270, 240, 64, "DomainMapping", "your own hostname"),
    ], edges: [E("svc", "cfg"), E("svc", "rt"), E("cfg", "rev"), E("rt", "rev", { dashed: true }), E("dm", "rt", { dashed: true })] },
    notes: "A Service owns a Configuration and a Route. Every change to the Configuration stamps a new immutable Revision; old ones stay addressable. The Route decides which revisions receive traffic and in what split. A DomainMapping attaches a hostname of your choosing, which is how we give one workload the same address in both regions." },
  // ---------------- 4c: scale to zero
  { kind: "content", chapter: 1, kicker: "Foundations", title: "Scaling to zero, and back",
    lines: ["KPA watches concurrency per revision", "An idle revision scales to zero", "The Activator holds the first request", "Pods come up, the request continues", "Bursts scale out to maxScale"],
    visual: { kind: "graph", nodes: [
      N("req", 20, 40, 170, 64, "request", "first one after idle"),
      N("act", 250, 40, 200, 64, "Activator", "buffers and wakes", "accent"),
      N("pods", 250, 220, 200, 80, "Revision pods", "0 → N → 0"),
      N("kpa", 20, 220, 170, 80, "KPA autoscaler", "concurrency → replicas"),
      N("ready", 480, 220, 100, 80, "Ready", "traffic direct"),
    ], edges: [E("req", "act"), E("act", "pods"), E("act", "kpa", { dashed: true }), E("kpa", "pods", { dashed: true }), E("pods", "ready")] },
    notes: "The Knative Pod Autoscaler measures in-flight requests per revision. When nothing arrives, the revision drops to zero pods and the Activator takes its place in the data path. The next request is held by the Activator, which asks the KPA for capacity; pods start, the request is forwarded, and once the revision is healthy the Activator steps out of the path. Bursts scale out up to the maxScale the API sets." },
  // ---------------- 5
  { kind: "content", chapter: 1, kicker: "Foundations", title: "The OpenShift Serverless Operator",
    lines: ["Red Hat's supported Knative distribution", "One KnativeServing custom resource", "OLM installs it and upgrades it", "Kourier ingress, Routes created for you", "Catalog mirrors with oc-mirror"],
    visual: { kind: "stack", layers: [["Knative Serving", "Service · Revision · Route · DomainMapping", "accent"], ["KnativeServing CR", "one document"], ["Serverless Operator", "reconciles, upgrades"], ["Operator Lifecycle Manager", "Subscription → catalog"], ["OpenShift", "one cluster per region"]] },
    notes: "The operator is Red Hat's packaging of Knative. One KnativeServing CR, reconciled by the operator, upgraded through OLM. Kourier is the ingress; Routes are created for us. The catalog mirrors with oc-mirror, which is what makes it viable in an airgap." },
  // ---------------- 6
  { kind: "content", chapter: 1, kicker: "Foundations", title: "Why the Operator",
    lines: ["Every alternative made us own more", "We never create a Route ourselves", "Airgap needs one supported mirror path"],
    visual: { kind: "table", head: ["", "Upstream Knative", "Serverless Operator"], rows: [
      ["Upgrades", "YAML we maintain", "OLM, from the catalog"],
      ["Ingress", "Run it ourselves", "Kourier, managed"],
      ["Routes, TLS", "Hand-made, RBAC", "Created for us"],
      ["Airgap", "Image by image", "oc-mirror, once"],
      ["Support", "Community", "Red Hat"],
    ] },
    notes: "The decision is about ownership. Upstream would mean maintaining the install, the ingress, the Routes and the mirror ourselves, with community support. The chart assumes the operator's conventions and the API holds no routes RBAC. Recorded in docs/DEPLOYING.md and the locked decisions in docs/ARCHITECTURE.md." },
  // ---------------- 7
  { kind: "section", chapter: 2, title: "The API", sub: "One HTTP call, a workload on every cluster." },
  // ---------------- 8
  { kind: "content", chapter: 2, kicker: "The API", title: "One call, every cluster",
    lines: ["Functions from git, containers from images", "Validate now, answer 202, poll status", "Closed status vocabulary, published on /info", "FastAPI, Pydantic, cloudlet-apis", "One Helm chart, rendered per region"],
    visual: { kind: "stats", items: [["2", "offerings"], ["202", "every write"], ["0", "kubectl for users"], ["2", "regions per deploy"]] },
    notes: "The API is a FastAPI control plane. Every write validates synchronously then returns 202 with a statusUrl; the status vocabulary is closed and published on /info so no client hardcodes it. One Helm chart, rendered by ArgoCD once per region." },
  // ---------------- 9
  { kind: "content", chapter: 2, kicker: "The API", title: "The endpoint structure",
    lines: ["23 endpoints under one base path", "The group is in the path", "OIDC token or admin key", "Streams use short-lived tickets"],
    visual: { kind: "code", lines: [
      ["/api/serverless/v1", "accent"],
      ["  /groups/{group}/functions", "ink"],
      ["    POST · GET · PUT · DELETE", "muted"],
      ["    /{name}/build · /stats · /pods · /logs", "muted"],
      ["  /groups/{group}/containers  →  /pull", "ink"],
      ["  /functions/info · /containers/info", "ink"],
      ["  /stream-tickets", "ink"],
    ] },
    notes: "Base path /api/serverless/v1. The SSO group is a path segment, so authorization is a path check against the groups claim. Ten endpoints per offering, two public info endpoints, one ticket mint. The Authorization header carries either a Keycloak JWT or the static admin key. EventSource cannot send headers, hence the 60-second HMAC ticket." },
  // ---------------- 10
  { kind: "content", chapter: 2, kicker: "The API", title: "Anatomy of a workload",
    lines: ["Identity: name, hostname, regions", "Source: git repo or image", "Runtime: env, files, port, size", "Scaling: min, max, metric, target", "Response: same shape, secrets redacted"],
    visual: { kind: "lifecycle", phases: ["Pending", "Building", "Deploying", "Ready"], failed: ["BuildFailed", "ImagePullFailed", "CrashLooping", "ConfigError", "ProgressDeadlineExceeded"] },
    notes: "Sizes map to CPU request-only and memory request equals limit. Concurrency and rps use the KPA and can scale to zero; cpu and memory switch to HPA. Failed always carries a machine-readable reason. PUT is a full replace but keeps redacted secrets when omitted." },
  // ---------------- 11
  { kind: "content", chapter: 2, kicker: "The API", title: "Talking to the cluster",
    lines: ["Client certificate mTLS, always", "No kubeconfig, no service-account token", "The certificate's CN is the user", "Address derived from the region name", "Every write is server-side apply"],
    visual: { kind: "graph", nodes: [
      N("cert", 20, 40, 250, 70, "cert-manager", "ACME, internal CA"),
      N("pod", 330, 40, 250, 70, "API pod", "tls.crt + tls.key"),
      N("k8s", 330, 200, 250, 70, "API server", "api.{cluster}.{domain}:6443"),
      N("rbac", 20, 200, 250, 70, "RBAC", "per tenant namespace"),
      N("cn", 60, 330, 480, 56, "CN = serverless-api.clients.{domain}", "the Kubernetes user", "accent"),
    ], edges: [E("cert", "pod"), E("pod", "k8s"), E("k8s", "rbac"), E("cn", "rbac", { dashed: true })] },
    notes: "No kubeconfig and no service-account path. cert-manager issues an ACME client certificate; its CN is a DNS name because ACME only issues to DNS identities, and that CN is the Kubernetes user RBAC binds. The API server address is derived from the region's cluster name. Every write is a server-side apply with force, so retries heal partial state." },
  // ---------------- 12
  { kind: "content", chapter: 2, kicker: "The API", title: "Active/active, two regions",
    lines: ["The API runs in both regions", "One deploy fans out to both", "No leader election, deterministic specs", "A region builds what it runs", "State lives in the Knative Service"],
    visual: { kind: "graph", nodes: [
      N("dns", 150, 10, 300, 60, "DNS", "*.serverless.{domain} → active region", "accent"),
      N("c", 20, 150, 260, 120, "central", "API · controllers · registry · workloads"),
      N("s", 320, 150, 260, 120, "south", "API · controllers · registry · workloads"),
      N("ksvc", 120, 340, 360, 56, "Knative Service = the truth", "no database, no replication"),
    ], edges: [E("dns", "c"), E("dns", "s"), E("c", "s", { dashed: true }), E("c", "ksvc", { dashed: true }), E("s", "ksvc", { dashed: true })] },
    notes: "Two OpenShift clusters trusting the same CA. The API runs in both; DNS fronts the active one. A deploy fans out to both concurrently and rolls up per-region results. No leader election: specs contain no timestamps, UUIDs or counters, so two writers converge. Each region builds into its own registry. The Knative Service and its annotations are the replicated truth." },
  // ---------------- 13
  { kind: "content", chapter: 2, kicker: "The API", title: "The tenant controller",
    lines: ["A namespace per SSO group", "Separate process, separate certificate", "The API never creates namespaces", "Provision before every deploy, fail closed", "Reconcile the local cluster only", "Garbage-collect empty namespaces, opt-in"],
    visual: { kind: "graph", nodes: [
      N("api", 20, 30, 220, 66, "API", "writes workloads only"),
      N("tc", 340, 30, 240, 66, "tenant controller", "namespaces, RBAC, policies", "accent"),
      N("nc", 20, 200, 260, 90, "central", "{group}-serverless"),
      N("ns", 320, 200, 260, 90, "south", "{group}-serverless"),
      N("tpl", 100, 340, 400, 56, "template set", "{{namespace}} {{group}} {{region}} {{registry}}"),
    ], edges: [E("api", "tc"), E("tc", "nc"), E("tc", "ns"), E("tpl", "tc", { dashed: true })] },
    notes: "Privilege separation is the reason: namespace and RBAC creation is cluster-scoped power the internet-facing API must not hold. Namespace per SSO group, identical in both clusters, rendered from region-neutral templates. Provision is called before every deploy and fails closed. Reconcile is local-cluster only so the regions never fight. The stamp protocol makes a converge crash-safe." },
  // ---------------- 14
  { kind: "section", chapter: 3, title: "Functions & builds", sub: "'Here is my repo' hides a build." },
  // ---------------- 15
  { kind: "content", chapter: 3, kicker: "Functions & builds", title: "Why functions are different",
    lines: ["Container: run this image", "Function: here is my repository", "A build takes minutes, not milliseconds", "A build is retried and logged", "Rebuilds fire on CVE patches, unasked"],
    visual: { kind: "graph", nodes: [
      N("img", 20, 30, 170, 60, "image", "container"),
      N("ksvc1", 400, 30, 180, 60, "Knative Service", "ready"),
      N("git", 20, 190, 170, 60, "git repo", "function", "accent"),
      N("build", 215, 190, 170, 60, "kpack build", "minutes"),
      N("ksvc2", 400, 190, 180, 60, "Knative Service", "digest rolled in"),
      N("cve", 215, 330, 170, 60, "CVE patch", "rebuilds everything", "accent"),
    ], edges: [E("img", "ksvc1"), E("git", "build"), E("build", "ksvc2"), E("cve", "build", { dashed: true })] },
    notes: "A container is run this image. A function is here is my repo, which means a build: asynchronous, retried, observable per phase, and fired automatically when a stack or buildpack is patched. That last property is why kpack was chosen over Tekton or func." },
  // ---------------- 16
  { kind: "content", chapter: 3, kicker: "Functions & builds", title: "Buildpacks and kpack",
    lines: ["No Dockerfile, buildpacks detect the language", "Rebase: patch the base without rebuilding", "kpack: Image, Build, Pod", "Our kpack repo is a Helm chart", "Airgap mirror scripts included", "ClusterBuilders: go, python, node"],
    visual: { kind: "graph", nodes: [
      N("stack", 20, 20, 170, 60, "ClusterStack", "base images"),
      N("store", 20, 120, 170, 60, "ClusterStore", "buildpackages"),
      N("order", 20, 220, 170, 60, "order", "component list"),
      N("builder", 250, 120, 190, 60, "ClusterBuilder", "go · python · node", "accent"),
      N("image", 250, 300, 190, 60, "Image", "declared by the API"),
      N("kp", 470, 20, 110, 260, "kpack chart", "mirror-once content"),
    ], edges: [E("stack", "builder"), E("store", "builder"), E("order", "builder"), E("builder", "image")] },
    notes: "No Dockerfile: Paketo buildpacks detect the language from the repo. Rebase swaps the run image without a rebuild. Our kpack repo is not a fork; it is a Helm chart packaging upstream 0.18 with the airgap pieces. The serverless chart composes the three ClusterBuilders because composing is a push." },
  // ---------------- 17
  { kind: "content", chapter: 3, kicker: "Functions & builds", title: "A build, step by step",
    lines: ["Image → SourceResolver → Build → Pod", "Each phase is its own container", "Each phase has its own log", "Pushed to this region's registry", "Five triggers; two need nobody"],
    visual: { kind: "phases", items: [["prepare", "git clone"], ["analyze", "previous image"], ["detect", "pick buildpacks"], ["restore", "cached layers"], ["build", "pip · npm · go"], ["export", "push image"], ["completion", "done"]] },
    notes: "Image to SourceResolver to Build to Pod. The CNB lifecycle runs as named init containers, so every phase has its own log. Five build reasons; BUILDPACK and STACK fire with no user action. Images go to the region registry; the layer cache is a registry tag. A Kyverno policy injects the CA into initContainers." },
  // ---------------- 18
  { kind: "content", chapter: 3, kicker: "Functions & builds", title: "The build controller",
    lines: ["Watches Image.status.latestImage", "Applies the digest onto the Service", "Both ends stay in one region", "Only ever writes digests", "Ships with no web stack"],
    visual: { kind: "graph", nodes: [
      N("im", 20, 60, 160, 70, "kpack Image", "latestImage changes"),
      N("bc", 220, 60, 160, 70, "build controller", "watch", "accent"),
      N("ks", 420, 60, 160, 70, "Knative Service", "image @sha256"),
      N("rev", 420, 240, 160, 70, "new Revision", "Knative rolls it out"),
      N("reg", 20, 240, 160, 70, "region registry", "the digest lives here"),
    ], edges: [E("im", "bc"), E("bc", "ks"), E("ks", "rev"), E("reg", "im", { dashed: true })] },
    notes: "The API declares a build and walks away. The build controller watches kpack Images in its own region and, when latestImage changes, server-side applies the live Knative Service with the digest. Exactly one writer per phase, and the controller only ever writes digests. It ships without a web stack, and CI proves it." },
  // ---------------- 19
  { kind: "section", chapter: 4, title: "Portal & the future", sub: "A console, and a library for the next API." },
  // ---------------- 20
  { kind: "content", chapter: 4, kicker: "Portal & the future", title: "How the portal wires it",
    lines: ["Next.js 16, server components", "Keycloak token stays server-side", "The SSO group is the project", "Forms are built from /info", "Live logs over SSE tickets", "Airgap-native: no CDN, no fonts"],
    visual: { kind: "graph", nodes: [
      N("br", 20, 60, 150, 70, "Browser", "React 19"),
      N("po", 225, 60, 150, 70, "Portal", "server actions", "accent"),
      N("ap", 430, 60, 150, 70, "Serverless API", "/api/serverless/v1"),
      N("sso", 225, 230, 150, 70, "Keycloak", "OIDC, groups claim"),
      N("tk", 20, 230, 150, 70, "?ticket=", "60 s, one path"),
    ], edges: [E("br", "po"), E("po", "ap"), E("po", "sso", { dashed: true }), E("tk", "ap", { dashed: true }), E("br", "tk", { dashed: true })] },
    notes: "Next.js 16 with server components and server actions in front of the API. The user's Keycloak token stays in the encrypted session and is forwarded server-side. The SSO group is the project. Forms are built from /info; the create flow turns 202 into a tracker toast. Live logs and stats stream over SSE using re-minted tickets." },
  // ---------------- 21
  { kind: "content", chapter: 4, kicker: "Portal & the future", title: "A console, not a Serverless UI",
    lines: ["Serverless is live, Storage is next", "An offering is data, not code", "The shell is already generic", "Next: shared layer, generated client"],
    visual: { kind: "tiles", items: ["Compute", "Serverless", "Databases", "Networking", "Object Storage", "Observability", "Data Integration", "Data Analytics", "Dev Tools", "Machine Learning", "Security", "…as data"], live: "Serverless", next: "Object Storage" },
    notes: "The catalog already names ten future offerings; Serverless is live and Object Storage has its env hook. Adding an offering is data, not code. The shell is generic; the serverless tree is the only product-specific part. Next: a shared resource layer and a generated TypeScript client so the contract stops being copied by hand." },
  // ---------------- 22
  { kind: "content", chapter: 4, kicker: "Portal & the future", title: "cloudlet-apis: start at step five",
    lines: ["Extracted from the Serverless API", "core, web and auth extras", "Layering enforced by tests", "A new API wires in five steps", "Same group rules everywhere"],
    visual: { kind: "stack", layers: [["[auth]", "OIDC · admin key · token proxy · tickets", "accent"], ["[web]", "health · error envelope · offline docs"], ["core", "errors · names · logging · request id"]] },
    notes: "cloudlet-apis is the shared Python library extracted from the Serverless API: core, web and auth as install extras, with layering enforced by tests. A new API wires it in five steps and inherits the security review. Two APIs normalizing groups differently is an authorization bug. Next extraction candidates: the mTLS multi-cluster client and the region fan-out." },
  // ---------------- 23
  { kind: "closing", kicker: "What we built", title: "Code in, a running service out.",
    stats: [["4", "repositories"], ["3", "services"], ["23", "endpoints"], ["2", "regions"], ["3", "runtimes"], ["0", "kubectl"]],
    lines: ["Operator owns Knative; we own the contract", "Deterministic specs, so no leader", "Builds declared, watched, rolled in", "The second product is cheaper"],
    notes: "Close on the four takeaways and open for questions." },
];

slides[1].visual.text = String(slides.length);
module.exports = { slides };
