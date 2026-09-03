const pptxgen = require("pptxgenjs");
const Fi = require("react-icons/fi");
const { gradientBg, icon } = require("./helpers");

// ---------- palette ----------
const C = {
  bg: "0B1120",
  panel: "121A2C",
  panel2: "18213A",
  line: "24304D",
  text: "F1F5F9",
  muted: "94A3B8",
  dim: "5B6B8A",
  cyan: "22D3EE",
  amber: "F59E0B",
  violet: "A78BFA",
  green: "34D399",
  rose: "FB7185",
};
const CH = {
  0: { accent: C.cyan, label: "" },
  1: { accent: C.cyan, label: "CHAPTER 01  ·  FOUNDATIONS" },
  2: { accent: C.cyan, label: "CHAPTER 02  ·  THE API" },
  3: { accent: C.amber, label: "CHAPTER 03  ·  FUNCTIONS & BUILDS" },
  4: { accent: C.violet, label: "CHAPTER 04  ·  THE PORTAL & WHAT COMES NEXT" },
};
const FONT_T = "Cambria";
const FONT_B = "Calibri";
const FONT_M = "Courier New";
const W = 13.333, H = 7.5, ML = 0.65, CW = 12.03;

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.title = "Serverless on Cloudlet";

const TOTAL = 23;
let slideNo = 0;
const bgs = {};
const icons = {};

async function prepare() {
  bgs.title = await gradientBg({ glows: [
    { cx: 1650, cy: 250, r: 1000, color: "#22D3EE", op: 0.35 },
    { cx: 200, cy: 1000, r: 900, color: "#A78BFA", op: 0.28 },
  ]});
  bgs.cyan = await gradientBg({ glows: [{ cx: 1800, cy: 0, r: 900, color: "#22D3EE", op: 0.22 }] });
  bgs.amber = await gradientBg({ glows: [{ cx: 1800, cy: 0, r: 900, color: "#F59E0B", op: 0.20 }] });
  bgs.violet = await gradientBg({ glows: [{ cx: 1800, cy: 0, r: 900, color: "#A78BFA", op: 0.22 }] });
  bgs.secCyan = await gradientBg({ glows: [{ cx: 300, cy: 900, r: 1100, color: "#22D3EE", op: 0.30 }] });
  bgs.secAmber = await gradientBg({ glows: [{ cx: 300, cy: 900, r: 1100, color: "#F59E0B", op: 0.28 }] });
  bgs.secViolet = await gradientBg({ glows: [{ cx: 300, cy: 900, r: 1100, color: "#A78BFA", op: 0.30 }] });
}
async function ic(name, color) {
  const key = name + color;
  if (!icons[key]) icons[key] = await icon(Fi[name], { color: "#" + color, size: 256 });
  return icons[key];
}
const bgFor = (ch) => (ch === 3 ? bgs.amber : ch === 4 ? bgs.violet : bgs.cyan);

// ---------- primitives ----------
function base(ch, { title, sub, bg } = {}) {
  const s = pres.addSlide();
  slideNo += 1;
  s.background = { data: bg || bgFor(ch) };
  const a = CH[ch].accent;
  if (CH[ch].label) {
    s.addText(CH[ch].label, { x: ML, y: 0.38, w: 8, h: 0.3, fontFace: FONT_B, fontSize: 10.5, color: a, charSpacing: 3, bold: true, margin: 0, isTextBox: true });
  }
  s.addText(`${String(slideNo).padStart(2, "0")} / ${TOTAL}`, { x: W - 1.7, y: H - 0.55, w: 1.05, h: 0.3, fontFace: FONT_M, fontSize: 9.5, color: C.dim, align: "right", margin: 0, isTextBox: true });
  if (title) s.addText(title, { x: ML, y: 0.72, w: CW, h: 0.8, fontFace: FONT_T, fontSize: 34, bold: true, color: C.text, margin: 0, isTextBox: true, valign: "middle" });
  if (sub) s.addText(sub, { x: ML, y: 1.5, w: CW - 0.5, h: 0.5, fontFace: FONT_B, fontSize: 15, color: C.muted, margin: 0, isTextBox: true, valign: "top", italic: true });
  return s;
}
const shadow = () => ({ type: "outer", color: "000000", blur: 8, offset: 3, angle: 90, opacity: 0.35 });

function panel(s, x, y, w, h, { fill = C.panel, line = C.line, radius = 0.12 } = {}) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: fill }, line: { color: line, width: 0.75 }, rectRadius: radius, shadow: shadow() });
}
async function badge(s, x, y, size, name, accent, { fillAlpha = 82 } = {}) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: size, h: size, fill: { color: accent, transparency: fillAlpha }, line: { color: accent, width: 0.5, transparency: 40 }, rectRadius: 0.1 });
  const pad = size * 0.24;
  s.addImage({ data: await ic(name, accent), x: x + pad, y: y + pad, w: size - 2 * pad, h: size - 2 * pad });
}
// card with icon badge, title, body (string or bullet array)
async function card(s, { x, y, w, h, accent, iconName, title, body, bodySize = 12.5, titleSize = 15 }) {
  panel(s, x, y, w, h);
  const bx = x + 0.22, by = y + 0.22;
  if (iconName) await badge(s, bx, by, 0.5, iconName, accent);
  const tx = iconName ? bx + 0.65 : bx;
  s.addText(title, { x: tx, y: by, w: w - (tx - x) - 0.2, h: 0.5, fontFace: FONT_B, fontSize: titleSize, bold: true, color: C.text, margin: 0, valign: "middle", isTextBox: true });
  const bodyY = by + 0.68;
  if (Array.isArray(body)) {
    const items = body.map((t, i) => ({ text: t, options: { bullet: { indent: 12 }, breakLine: i < body.length - 1, paraSpaceAfter: 5 } }));
    s.addText(items, { x: bx, y: bodyY, w: w - 0.44, h: h - (bodyY - y) - 0.15, fontFace: FONT_B, fontSize: bodySize, color: C.muted, margin: 0, valign: "top", isTextBox: true });
  } else if (body) {
    s.addText(body, { x: bx, y: bodyY, w: w - 0.44, h: h - (bodyY - y) - 0.15, fontFace: FONT_B, fontSize: bodySize, color: C.muted, margin: 0, valign: "top", isTextBox: true });
  }
}
// icon row: badge left, bold heading, description under
async function iconRow(s, { x, y, w, accent, iconName, head, text, h = 0.95, textSize = 12.5 }) {
  await badge(s, x, y + 0.02, 0.56, iconName, accent);
  s.addText(head, { x: x + 0.78, y, w: w - 0.78, h: 0.32, fontFace: FONT_B, fontSize: 15, bold: true, color: C.text, margin: 0, isTextBox: true, valign: "middle" });
  s.addText(text, { x: x + 0.78, y: y + 0.33, w: w - 0.78, h: h - 0.33, fontFace: FONT_B, fontSize: textSize, color: C.muted, margin: 0, isTextBox: true, valign: "top" });
}
function chip(s, x, y, w, h, text, { accent = C.cyan, size = 11, mono = false, fillAlpha = 84, bold = true, color, solid = false } = {}) {
  if (solid) s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: C.panel }, line: { color: accent, width: 0.75 }, rectRadius: 0.08 });
  else s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: accent, transparency: fillAlpha }, line: { color: accent, width: 0.5, transparency: 45 }, rectRadius: 0.08 });
  s.addText(text, { x, y, w, h, fontFace: mono ? FONT_M : FONT_B, fontSize: size, bold, color: color || accent, align: "center", valign: "middle", margin: 0.04, isTextBox: true });
}
function node(s, x, y, w, h, title, sub, { accent = C.cyan, fill = C.panel2, titleSize = 13, subSize = 10 } = {}) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: fill }, line: { color: accent, width: 1 }, rectRadius: 0.1, shadow: shadow() });
  if (sub) {
    s.addText([
      { text: title, options: { fontSize: titleSize, bold: true, color: C.text, breakLine: true } },
      { text: sub, options: { fontSize: subSize, color: C.muted } },
    ], { x, y, w, h, fontFace: FONT_B, align: "center", valign: "middle", margin: 0.06, isTextBox: true });
  } else {
    s.addText(title, { x, y, w, h, fontFace: FONT_B, fontSize: titleSize, bold: true, color: C.text, align: "center", valign: "middle", margin: 0.06, isTextBox: true });
  }
}
function arrow(s, x1, y1, x2, y2, { color = "5B6B8A", width = 1.5, dash = "solid", head = true } = {}) {
  const opts = { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1), line: { color, width, dashType: dash } };
  if (head) opts.line.endArrowType = "triangle";
  if (x2 < x1 && y2 === y1) opts.flipH = true;
  if (y2 < y1 && x2 === x1) opts.flipV = true;
  if (x2 < x1 && y2 !== y1) opts.flipH = true;
  if (y2 < y1 && x2 !== x1) opts.flipV = true;
  s.addShape(pres.shapes.LINE, opts);
}
function label(s, x, y, w, h, text, { size = 10, color = C.muted, align = "center", mono = false, bold = false, italic = false, valign = "middle" } = {}) {
  s.addText(text, { x, y, w, h, fontFace: mono ? FONT_M : FONT_B, fontSize: size, color, align, valign, margin: 0, isTextBox: true, bold, italic });
}
function stat(s, x, y, w, big, small, accent) {
  s.addText(big, { x, y, w, h: 0.85, fontFace: FONT_T, fontSize: 48, bold: true, color: accent, margin: 0, isTextBox: true, valign: "bottom" });
  s.addText(small, { x, y: y + 0.88, w, h: 0.5, fontFace: FONT_B, fontSize: 12.5, color: C.muted, margin: 0, isTextBox: true, valign: "top" });
}
function section(ch, num, title, blurb, bg) {
  const s = pres.addSlide();
  slideNo += 1;
  s.background = { data: bg };
  const a = CH[ch].accent;
  s.addText(num, { x: ML, y: 1.3, w: 6, h: 2.4, fontFace: FONT_T, fontSize: 150, bold: true, color: a, margin: 0, isTextBox: true, valign: "middle" });
  s.addText(title, { x: ML, y: 3.8, w: 10, h: 1, fontFace: FONT_T, fontSize: 44, bold: true, color: C.text, margin: 0, isTextBox: true });
  s.addText(blurb, { x: ML, y: 4.85, w: 8.5, h: 1, fontFace: FONT_B, fontSize: 17, color: C.muted, margin: 0, isTextBox: true, italic: true });
  s.addText(`${String(slideNo).padStart(2, "0")} / ${TOTAL}`, { x: W - 1.7, y: H - 0.55, w: 1.05, h: 0.3, fontFace: FONT_M, fontSize: 9.5, color: C.dim, align: "right", margin: 0, isTextBox: true });
  return s;
}
function codeBlock(s, x, y, w, h, lines, { size = 10.5, accent = C.cyan } = {}) {
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: "080D19" }, line: { color: C.line, width: 0.75 }, rectRadius: 0.1, shadow: shadow() });
  const runs = [];
  lines.forEach((ln, i) => {
    const parts = Array.isArray(ln) ? ln : [{ t: ln }];
    parts.forEach((p, j) => runs.push({ text: p.t, options: { color: p.c || C.text, bold: !!p.b, breakLine: j === parts.length - 1 && i < lines.length - 1 } }));
  });
  s.addText(runs, { x: x + 0.22, y: y + 0.18, w: w - 0.4, h: h - 0.3, fontFace: FONT_M, fontSize: size, margin: 0, valign: "top", isTextBox: true, lineSpacingMultiple: 1.12 });
}


const NOTES = [
  "Open with the one-sentence promise: a developer gives us code or an image and gets a running, addressable service in both regions without ever seeing Kubernetes. Everything that follows explains how that sentence is kept.",
  "Four chapters. Foundations is the vocabulary. The API is the contract. Functions and builds is where most of the engineering went. Portal and future is what the platform becomes next.",
  "Serverless is a billing and operations model, not a technology. Scale to zero is the defining property: an idle workload costs nothing and needs nobody. The chart is illustrative, not measured.",
  "Knative Serving: a Service owns a Configuration and a Route. Every change to the Configuration stamps an immutable Revision. The KPA autoscaler scales revisions on concurrency, including to zero; the Activator buffers requests while a revision wakes. We do not expose Eventing. DomainMapping is the piece that lets us give one workload the same host in both clusters.",
  "The operator is Red Hat's packaging of Knative. One KnativeServing CR, reconciled by the operator, upgraded through OLM. Kourier is the ingress; Routes are created for us. The catalog mirrors with oc-mirror, which is what makes it viable in an airgap.",
  "The decision is about ownership. Upstream would mean maintaining the install, the ingress, the Routes and the mirror ourselves, with community support. The chart assumes the operator's conventions and the API deliberately holds no routes RBAC. This is recorded in docs/DEPLOYING.md and the locked decisions table in docs/ARCHITECTURE.md.",
  "",
  "The API is a FastAPI control plane. Two offerings: functions from git, containers from an image. Every write validates synchronously then returns 202 with a statusUrl; the status vocabulary is closed and published on /info so no client hardcodes it. One Helm chart, rendered by ArgoCD once per region from a central GitOps repo.",
  "Base path /api/serverless/v1. The SSO group is a path segment, so authorization is a path check against the groups claim. Ten endpoints per offering, two public info endpoints, one ticket mint. Same Authorization header carries either a Keycloak JWT or the static admin key, told apart by shape. EventSource cannot send headers, hence the 60-second HMAC ticket.",
  "Walk the request fields: identity, source, runtime, scaling. Sizes map to CPU request-only and memory request equals limit. Concurrency and rps use the KPA and can scale to zero; cpu and memory switch to HPA. Failed always carries a machine-readable reason. PUT is a full replace but keeps redacted secrets when omitted.",
  "There is no kubeconfig and no service-account path. cert-manager issues an ACME client certificate; its CN is a DNS name because ACME only issues to DNS identities, and that CN is the Kubernetes user RBAC binds. The API server address is derived from the region's cluster name. Every write is a server-side apply with force, so retries heal partial state.",
  "Two OpenShift clusters, both trusting the same CA. The API runs in both; DNS fronts the active one. A deploy fans out to both concurrently and rolls up per-region results. No leader election: specs contain no timestamps, UUIDs or counters, so two writers converge on one object. Each region builds into its own registry. There is no database; the Knative Service and its annotations are the replicated truth.",
  "Privilege separation is the reason this is a separate process: namespace and RBAC creation is cluster-scoped power the internet-facing API must not hold. Namespace per SSO group, identical in both clusters, rendered from region-neutral templates. Provision is called before every deploy and fails closed. Reconcile is local-cluster only so the regions never fight. The stamp protocol makes a converge crash-safe.",
  "",
  "A container is run this image. A function is here is my repo, which means a build: asynchronous, retried, observable per phase, and fired automatically when a stack or buildpack is patched. That last property is why kpack was chosen over Tekton or func.",
  "No Dockerfile: Paketo buildpacks detect the language from the repo. Rebase swaps the run image without a rebuild. Our kpack repo is not a fork; it is a Helm chart packaging upstream 0.18 with the airgap pieces: CRDs as templates, cluster build content, and mirror scripts that also pull the runtimes buildpacks download at build time. The serverless chart composes the three ClusterBuilders because composing is a push.",
  "Image to SourceResolver to Build to Pod. The CNB lifecycle runs as named init containers, so every phase has its own log. Five build reasons; BUILDPACK and STACK fire with no user action. Images go to the region registry under group and name; the layer cache is a registry tag. A Kyverno policy injects the CA into initContainers; the dependency mirror uses originalHost because upstream hosts differ.",
  "The API declares a build and walks away. The build controller watches kpack Images in its own region and, when latestImage changes, server-side applies the live Knative Service with the digest. Both ends are local. Exactly one writer per phase, and the controller only ever writes digests. It ships without a web stack, and CI proves it.",
  "",
  "Next.js 16 with server components and server actions in front of the API. The user's Keycloak token stays in the encrypted session and is forwarded server-side. The SSO group is the project. Forms are built from /info; the create flow turns 202 into a tracker toast. Live logs and stats stream over SSE using re-minted tickets and fall back to polling honestly. Everything renders in an airgap.",
  "The catalog already names ten future offerings; Serverless is live and Object Storage has its env hook. Adding an offering is data, not code. The shell is generic; the serverless tree is the only product-specific part. The next step is a shared resource layer and a generated TypeScript client so the contract stops being copied by hand.",
  "cloudlet-apis is the shared Python library extracted from the Serverless API: core, web and auth as install extras, with layering enforced by tests. A new API wires it in five steps and inherits the security review. The reason it exists: two APIs normalizing groups differently is an authorization bug. Next extraction candidates are the mTLS multi-cluster client and the region fan-out.",
  "Close on the four takeaways and open for questions.",
];
// ============================================================
async function build() {
  await prepare();
  const cy = C.cyan, am = C.amber, vi = C.violet;

  // ---------- 1. Title ----------
  {
    const s = pres.addSlide(); slideNo += 1;
    s.background = { data: bgs.title };
    s.addText("SERVERLESS ON CLOUDLET", { x: ML, y: 1.75, w: 8, h: 0.35, fontFace: FONT_B, fontSize: 12, bold: true, color: cy, charSpacing: 4, margin: 0, isTextBox: true });
    s.addText("From Knative\nto the Portal", { x: ML, y: 2.15, w: 8.2, h: 2.3, fontFace: FONT_T, fontSize: 62, bold: true, color: C.text, margin: 0, isTextBox: true, valign: "middle", lineSpacingMultiple: 0.95 });
    s.addText("What we built, how it fits together, and why we built it this way.", { x: ML, y: 4.55, w: 7.6, h: 0.6, fontFace: FONT_B, fontSize: 18, color: C.muted, margin: 0, isTextBox: true, italic: true });
    s.addText("Team review  ·  September 2026", { x: ML, y: 5.35, w: 6, h: 0.35, fontFace: FONT_M, fontSize: 11, color: C.dim, margin: 0, isTextBox: true });
    // constellation: two regions + api + portal + registry nodes
    const pts = {
      portal: [10.2, 1.7], api: [10.9, 3.2], central: [9.6, 4.9], south: [12.2, 4.6], reg: [12.1, 2.4], build: [8.6, 3.0],
    };
    const edges = [["portal", "api"], ["api", "central"], ["api", "south"], ["api", "reg"], ["build", "central"], ["build", "api"], ["central", "south"]];
    edges.forEach(([a, b]) => arrow(s, pts[a][0], pts[a][1], pts[b][0], pts[b][1], { color: "2C3A5C", width: 1.25, head: false }));
    const nodesIcons = { portal: ["FiMonitor", vi], api: ["FiCloud", cy], central: ["FiServer", cy], south: ["FiServer", cy], reg: ["FiPackage", am], build: ["FiTool", am] };
    for (const [k, [x, y]] of Object.entries(pts)) {
      const [n, col] = nodesIcons[k]; const sz = k === "api" ? 0.9 : 0.68;
      s.addShape(pres.shapes.OVAL, { x: x - sz / 2, y: y - sz / 2, w: sz, h: sz, fill: { color: C.bg }, line: { color: col, width: 1.25 }, shadow: shadow() });
      const p = sz * 0.27;
      s.addImage({ data: await ic(n, col), x: x - sz / 2 + p, y: y - sz / 2 + p, w: sz - 2 * p, h: sz - 2 * p });
    }
    label(s, 9.0, 5.3, 1.2, 0.3, "central", { mono: true, size: 9, color: C.dim });
    label(s, 11.6, 5.0, 1.2, 0.3, "south", { mono: true, size: 9, color: C.dim });
  }

  // ---------- 2. Journey ----------
  {
    const s = base(0, { title: "The journey, in four chapters", sub: "Fourteen questions, one platform. We start at the idea and end at the console." });
    const chapters = [
      { n: "01", t: "Foundations", a: cy, i: "FiCompass", items: ["What is serverless", "What is Knative", "The OpenShift Serverless Operator", "Why we chose it"] },
      { n: "02", t: "The API", a: cy, i: "FiCloud", items: ["Purpose and structure", "Talking to the cluster with mTLS", "Active/active across two regions", "The tenant controller"] },
      { n: "03", t: "Functions & builds", a: am, i: "FiTool", items: ["Why functions are different", "Buildpacks and kpack", "A build, step by step", "The build controller"] },
      { n: "04", t: "Portal & future", a: vi, i: "FiMonitor", items: ["How the portal wires it together", "The portal beyond serverless", "cloudlet-apis: the next API"] },
    ];
    const w = (CW - 0.9) / 4;
    for (let k = 0; k < 4; k++) {
      const c = chapters[k]; const x = ML + k * (w + 0.3), y = 2.25, h = 4.2;
      panel(s, x, y, w, h);
      s.addText(c.n, { x: x + 0.25, y: y + 0.2, w: 1.5, h: 0.8, fontFace: FONT_T, fontSize: 40, bold: true, color: c.a, margin: 0, isTextBox: true });
      await badge(s, x + w - 0.85, y + 0.3, 0.6, c.i, c.a);
      s.addText(c.t, { x: x + 0.25, y: y + 1.05, w: w - 0.5, h: 0.45, fontFace: FONT_B, fontSize: 18, bold: true, color: C.text, margin: 0, isTextBox: true });
      const items = c.items.map((t, i) => ({ text: t, options: { bullet: { indent: 12 }, breakLine: i < c.items.length - 1, paraSpaceAfter: 7 } }));
      s.addText(items, { x: x + 0.25, y: y + 1.6, w: w - 0.5, h: h - 1.8, fontFace: FONT_B, fontSize: 13, color: C.muted, margin: 0, valign: "top", isTextBox: true });
    }
  }

  // ---------- 3. What is serverless ----------
  {
    const s = base(1, { title: "What is serverless?", sub: "You bring code. The platform brings the servers, the scaling, and a bill only for what actually runs." });
    const rows = [
      ["FiZap", "Scale to zero, and back", "An idle workload costs nothing. The first request wakes it; a burst fans it out to as many replicas as the traffic needs."],
      ["FiServer", "No servers to own", "No VMs to patch, no capacity to plan, no cluster to learn. You deploy a unit of work, not a machine."],
      ["FiActivity", "Driven by requests", "Every instance exists because traffic asked for it, and disappears when the traffic stops. The unit of billing is the request, not the hour."],
    ];
    let y = 2.3;
    for (const [i, h, t] of rows) { await iconRow(s, { x: ML, y, w: 5.6, accent: cy, iconName: i, head: h, text: t, h: 1.15 }); y += 1.35; }
    // native chart: replicas follow traffic
    panel(s, 6.9, 2.25, 5.78, 4.2);
    s.addText("Replicas follow traffic", { x: 7.15, y: 2.4, w: 5, h: 0.35, fontFace: FONT_B, fontSize: 14, bold: true, color: C.text, margin: 0, isTextBox: true });
    s.addText("A function's day: nothing, a burst, nothing again.", { x: 7.15, y: 2.72, w: 5.2, h: 0.3, fontFace: FONT_B, fontSize: 11, color: C.muted, margin: 0, isTextBox: true, italic: true });
    const labels = ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00", "24:00"];
    s.addChart(pres.charts.AREA, [{ name: "Replicas", labels, values: [0, 0, 1, 6, 11, 7, 3, 0, 0] }], {
      x: 7.05, y: 3.05, w: 5.5, h: 3.3, chartColors: [cy], chartColorsOpacity: 35, lineSize: 2.5, lineDataSymbol: "none",
      catAxisLabelColor: C.dim, valAxisLabelColor: C.dim, catAxisLabelFontSize: 9, valAxisLabelFontSize: 9, catAxisLabelFontFace: FONT_M, valAxisLabelFontFace: FONT_M,
      valGridLine: { color: "1C2740", size: 0.5 }, catGridLine: { style: "none" }, showLegend: false, showTitle: false, valAxisMaxVal: 12, valAxisMinVal: 0,
      catAxisLineShow: false, valAxisLineShow: false, plotArea: { fill: { color: C.panel } }, chartArea: { fill: { color: C.panel } },
    });
  }

  // ---------- 4. What is Knative ----------
  {
    const s = base(1, { title: "What is Knative?", sub: "The Kubernetes-native building blocks for serverless. We use Serving: one object in, a routed, autoscaled, versioned workload out." });
    // diagram left
    const dx = ML, dy = 2.3;
    node(s, dx + 1.6, dy, 2.9, 0.8, "Knative Service", "serving.knative.dev/v1 · the one object you write", { accent: cy });
    arrow(s, dx + 2.3, dy + 0.8, dx + 1.3, dy + 1.5);
    arrow(s, dx + 3.8, dy + 0.8, dx + 4.8, dy + 1.5);
    node(s, dx, dy + 1.5, 2.6, 0.8, "Configuration", "desired state of the code", { accent: cy });
    node(s, dx + 3.5, dy + 1.5, 2.6, 0.8, "Route", "traffic → revisions, 100% or split", { accent: cy });
    arrow(s, dx + 1.3, dy + 2.3, dx + 1.3, dy + 3.0);
    node(s, dx, dy + 3.0, 2.6, 0.85, "Revision N", "immutable snapshot: image + config", { accent: cy, fill: C.panel });
    arrow(s, dx + 3.5, dy + 3.4, dx + 2.6, dy + 3.4, { dash: "dash" });
    node(s, dx + 3.5, dy + 3.0, 2.6, 0.85, "Autoscaler (KPA)", "concurrency in → replicas out, 0…N", { accent: C.green });
    label(s, dx, dy + 3.95, 6.1, 0.3, "The Activator holds requests while a revision scales up from zero.", { size: 10.5, italic: true, color: C.dim, align: "left" });
    // right: three cards
    const cards = [
      ["FiLayers", "Serving", "Request-driven workloads with scale-to-zero, revision history and traffic routing. The half we run in production."],
      ["FiSend", "Eventing", "Brokers, triggers and CloudEvents for event-driven flows. Knative ships it; our platform does not expose it yet."],
      ["FiGlobe", "DomainMapping", "Your own hostname on a Service. It becomes the way we give one workload the same address in both regions."],
    ];
    let y = 2.3;
    for (const [i, t, b] of cards) { await card(s, { x: 7.45, y, w: 5.23, h: 1.42, accent: cy, iconName: i, title: t, body: b, bodySize: 11.5 }); y += 1.52; }
  }

  // ---------- 5. OpenShift Serverless Operator ----------
  {
    const s = base(1, { title: "What is the OpenShift Serverless Operator?", sub: "Red Hat's supported distribution of Knative, installed, configured and upgraded by the Operator Lifecycle Manager." });
    const rows = [
      ["FiPackage", "One CR describes Knative", "A KnativeServing custom resource in the knative-serving namespace. The operator installs every component from it and upgrades them with the catalog."],
      ["FiGlobe", "Kourier ingress and real Routes", "Ingress runs in knative-serving-ingress. Every Knative Service and DomainMapping gets an OpenShift Route created for it, edge-terminated with the cluster wildcard."],
      ["FiShield", "Supported and mirrorable", "OLM catalog images mirror with oc-mirror, which is how a supported Knative reaches an airgapped datacenter at all."],
    ];
    let y = 2.3;
    for (const [i, h, t] of rows) { await iconRow(s, { x: ML, y, w: 6.5, accent: cy, iconName: i, head: h, text: t, h: 1.3 }); y += 1.4; }
    // right: layer stack
    const sx = 8.1, sw = 4.55;
    const layers = [
      ["Knative Serving", "Service · Revision · Route · autoscaler · DomainMapping", cy, C.panel2],
      ["KnativeServing CR", "one document: ingress class, domains, autoscaling defaults", cy, C.panel],
      ["OpenShift Serverless Operator", "reconciles the CR, owns upgrades", cy, C.panel],
      ["Operator Lifecycle Manager", "Subscription → CSV → catalog", "5B6B8A", C.panel],
      ["OpenShift", "the cluster, in each region", "5B6B8A", C.panel],
    ];
    let ly = 2.3;
    for (const [t, sub, a, f] of layers) { node(s, sx, ly, sw, 0.72, t, sub, { accent: a, fill: f, titleSize: 13, subSize: 9.5 }); ly += 0.82; }
  }

  // ---------- 6. Why the operator ----------
  {
    const s = base(1, { title: "Why the Operator, not upstream Knative", sub: "Every alternative made us own something Red Hat already owns. In an airgapped datacenter, ownership is expensive." });
    const rows = [
      ["Install and upgrades", "Helm or YAML we maintain, version by version", "An OLM Subscription; the catalog carries the upgrade"],
      ["Ingress", "Pick and run kourier or Istio ourselves", "Kourier, managed, in knative-serving-ingress"],
      ["Routes and TLS", "Hand-made Routes with RBAC to write them", "Operator-created, edge-terminated, no routes RBAC for us"],
      ["Airgap", "Mirror each image by hand", "oc-mirror the catalog, once"],
      ["Support", "Community issues and our own patches", "Red Hat, on the OpenShift we already run"],
    ];
    const x0 = ML, y0 = 2.3, c1 = 2.5, c2 = 4.6, c3 = 4.6, rh = 0.62;
    label(s, x0 + c1, y0, c2, 0.4, "UPSTREAM / COMMUNITY KNATIVE", { size: 10.5, bold: true, color: C.dim, align: "left" });
    label(s, x0 + c1 + c2 + 0.33, y0, c3, 0.4, "OPENSHIFT SERVERLESS OPERATOR", { size: 10.5, bold: true, color: cy, align: "left" });
    let y = y0 + 0.45;
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: x0 + c1 + c2 + 0.2, y: y - 0.08, w: c3 + 0.26, h: rows.length * rh + 0.1, fill: { color: cy, transparency: 90 }, line: { color: cy, width: 0.75, transparency: 50 }, rectRadius: 0.1 });
    for (const [k, a, b] of rows) {
      label(s, x0, y, c1, rh, k, { size: 13, bold: true, color: C.text, align: "left" });
      label(s, x0 + c1, y, c2, rh, a, { size: 12, color: C.muted, align: "left" });
      label(s, x0 + c1 + c2 + 0.33, y, c3, rh, b, { size: 12, color: C.text, align: "left" });
      y += rh;
      if (k !== "Support") s.addShape(pres.shapes.LINE, { x: x0, y, w: c1 + c2 + c3 + 0.33, h: 0, line: { color: C.line, width: 0.5 } });
    }
    panel(s, ML, 6.0, CW, 0.85, { fill: C.panel });
    await badge(s, ML + 0.2, 6.17, 0.5, "FiAnchor", cy);
    s.addText([
      { text: "Our chart assumes the operator's conventions. ", options: { bold: true, color: C.text } },
      { text: "Kourier in knative-serving-ingress, operator-managed Routes, DomainMapping for hosts. The API never creates a Route and holds no routes RBAC: that is the operator's job.", options: { color: C.muted } },
    ], { x: ML + 0.85, y: 6.05, w: CW - 1.1, h: 0.75, fontFace: FONT_B, fontSize: 12, margin: 0, valign: "middle", isTextBox: true });
  }

  // ---------- 7. Section: API ----------
  section(2, "02", "The API", "A Python control plane that turns one HTTP call into a workload on every cluster.", bgs.secCyan);

  // ---------- 8. API purpose ----------
  {
    const s = base(2, { title: "The API: one call, every cluster", sub: "Customers describe a workload. The API turns it into Knative objects on both clusters and reports back a single status." });
    const stats = [["2", "offerings: functions and containers"], ["202", "every write is asynchronous"], ["0", "kubectl. Customers never see Kubernetes"], ["2", "regions in every deploy"]];
    const sw = (CW - 0.9) / 4;
    stats.forEach(([b, t], k) => stat(s, ML + k * (sw + 0.3), 2.2, sw, b, t, cy));
    const cards = [
      ["FiEyeOff", "What it hides", ["Knative Services and DomainMappings", "kpack Images, git and registry secrets", "Namespaces, RBAC, NetworkPolicies", "Two clusters that must agree"]],
      ["FiCheckCircle", "What it guarantees", ["Validation up front, then 202 with a statusUrl", "A closed status vocabulary: Pending · Building · Deploying · Ready · Failed · Terminating", "Machine-readable reasons and one error envelope with a request id"]],
      ["FiCode", "What it is built with", ["FastAPI + Pydantic v2, the official kubernetes client", "cloudlet-apis for auth, errors, logging, docs", "37 test modules, ruff, layering tests", "One Helm chart, rendered by ArgoCD once per region"]],
    ];
    const cw = (CW - 0.6) / 3;
    for (let k = 0; k < 3; k++) { const [i, t, b] = cards[k]; await card(s, { x: ML + k * (cw + 0.3), y: 3.85, w: cw, h: 2.95, accent: cy, iconName: i, title: t, body: b, bodySize: 12 }); }
  }

  // ---------- 9. API structure ----------
  {
    const s = base(2, { title: "The API structure", sub: "Everything hangs off one base path. The SSO group is a path segment on every workload call, never a body field." });
    const K = { c: cy, b: true }, M = { c: C.muted }, G = { c: C.green, b: true }, Y = { c: am };
    const lines = [
      [{ t: "/api/serverless/v1", ...K }],
      [{ t: "├── /groups/{group}/functions", c: C.text, b: true }],
      [{ t: "│     POST                ", ...M }, { t: "create → 202", ...G }],
      [{ t: "│     GET                 ", ...M }, { t: "list  ?sort=name|createdAt", c: C.text }],
      [{ t: "│     GET     /{name}     ", ...M }, { t: "read, secrets redacted", c: C.text }],
      [{ t: "│     PUT     /{name}     ", ...M }, { t: "full replace → 202", ...G }],
      [{ t: "│     DELETE  /{name}     ", ...M }, { t: "→ 204", ...G }],
      [{ t: "│     POST    /{name}/build", ...M }, { t: "   rebuild → 202", ...G }],
      [{ t: "│     GET     /{name}/stats  · /stats/stream", ...M }],
      [{ t: "│     GET     /{name}/pods   · /logs/pods/{pod}", ...M }],
      [{ t: "├── /groups/{group}/containers", c: C.text, b: true }, { t: "   same set; /pull instead of /build", ...M }],
      [{ t: "├── /functions/info · /containers/info", c: C.text, b: true }, { t: "   public capabilities", ...M }],
      [{ t: "├── /stream-tickets", c: C.text, b: true }, { t: "   HMAC ticket for EventSource", ...M }],
      [{ t: "└── /healthz · /readyz · /docs · /openapi.json", ...M }],
    ];
    codeBlock(s, ML, 2.25, 7.4, 4.55, lines, { size: 11.5 });
    const facts = [
      ["FiHash", "23 endpoints", "10 for functions, 10 for containers, two public info endpoints and the ticket mint."],
      ["FiKey", "One header, two credentials", "A Keycloak OIDC bearer token, validated offline against cached JWKS, or a static admin key, told apart by shape."],
      ["FiUsers", "403 outside your {group}", "Authorization is the groups claim. Names are normalized in one place; admin groups may act for anyone."],
      ["FiRadio", "Streams get tickets", "EventSource cannot send a header, so the browser gets a 60-second, single-path HMAC ticket instead."],
    ];
    let y = 2.25;
    for (const [i, h, t] of facts) { await iconRow(s, { x: 8.45, y, w: 4.25, accent: cy, iconName: i, head: h, text: t, h: 1.05, textSize: 11.5 }); y += 1.15; }
  }

  // ---------- 10. Anatomy of a workload ----------
  {
    const s = base(2, { title: "Anatomy of a workload", sub: "The same body shape for functions and containers, one closed lifecycle, and a response that is the request with secrets redacted." });
    // left: request fields
    panel(s, ML, 2.25, 6.6, 4.55);
    s.addText("THE REQUEST", { x: ML + 0.25, y: 2.4, w: 3, h: 0.3, fontFace: FONT_B, fontSize: 10.5, bold: true, color: cy, charSpacing: 3, margin: 0, isTextBox: true });
    const groups = [
      ["Identity", "name · hostname · regions[]"],
      ["Source (function)", "gitRepo · branch · path · gitToken · runtime · version"],
      ["Source (container)", "image · registryUsername · registryToken"],
      ["Runtime", "env[] with secret flag · files[] text or base64 · port · size small | medium | large"],
      ["Scaling", "minScale · maxScale · metric concurrency | rps | cpu | memory · target · scaleDownDelay"],
    ];
    let y = 2.8;
    for (const [g, f] of groups) {
      label(s, ML + 0.25, y, 1.7, 0.72, g, { size: 12.5, bold: true, color: C.text, align: "left", valign: "top" });
      s.addText(f, { x: ML + 2.0, y, w: 4.35, h: 0.72, fontFace: FONT_M, fontSize: 10, color: C.muted, margin: 0, valign: "top", isTextBox: true });
      y += 0.78;
    }
    // right: lifecycle
    const rx = 7.55;
    label(s, rx, 2.3, 4, 0.3, "THE LIFECYCLE", { size: 10.5, bold: true, color: cy, align: "left" });
    const phases = ["Pending", "Building", "Deploying", "Ready"];
    const pw = 1.12; let px = rx;
    phases.forEach((p, k) => {
      chip(s, px, 2.7, pw, 0.42, p, { accent: k === 3 ? C.green : cy, size: 11.5, fillAlpha: k === 3 ? 78 : 84 });
      if (k < 3) arrow(s, px + pw, 2.91, px + pw + 0.22, 2.91, { width: 1.25 });
      px += pw + 0.22;
    });
    label(s, rx, 3.18, 5.1, 0.3, "Building only exists for functions. Terminating follows a DELETE.", { size: 10.5, italic: true, color: C.dim, align: "left" });
    chip(s, rx, 3.6, 1.12, 0.42, "Failed", { accent: C.rose, size: 11.5, fillAlpha: 82 });
    label(s, rx + 1.3, 3.55, 3.9, 0.55, "with a machine-readable reason:", { size: 11.5, color: C.muted, align: "left" });
    const reasons = ["BuildFailed", "ImagePullFailed", "CrashLooping", "ConfigError", "ProgressDeadlineExceeded"];
    let rxx = rx, ryy = 4.15;
    reasons.forEach((r) => { const w = 0.16 + r.length * 0.075; if (rxx + w > 12.7) { rxx = rx; ryy += 0.42; } chip(s, rxx, ryy, w, 0.34, r, { accent: C.rose, size: 9.5, mono: true, fillAlpha: 90, bold: false }); rxx += w + 0.12; });
    await card(s, { x: rx, y: 4.95, w: 5.13, h: 1.85, accent: cy, iconName: "FiFileText", title: "The response", body: "The request with secrets redacted, plus status, hostname, createdAt and one row per region: status, revision, reason, message, replicas. Omit a redacted secret on PUT and it is kept, so read-modify-write never strips credentials.", bodySize: 11 });
  }

  // ---------- 11. mTLS ----------
  {
    const s = base(2, { title: "How the API talks to the cluster", sub: "Client-certificate mTLS, always. No kubeconfig, no service-account token, no per-region URL to configure." });
    const y = 2.45, h = 1.05;
    node(s, ML, y, 2.6, h, "cert-manager Certificate", "ACME, issued by the internal CA", { accent: cy });
    arrow(s, ML + 2.6, y + h / 2, ML + 2.95, y + h / 2);
    node(s, ML + 2.95, y, 2.7, h, "tls.crt + tls.key", "mounted in the API pod", { accent: cy });
    arrow(s, ML + 5.65, y + h / 2, ML + 6.0, y + h / 2);
    node(s, ML + 6.0, y, 3.3, h, "Kubernetes API server", "https://api.{cluster}.{base_domain}:6443", { accent: cy, subSize: 9.5 });
    arrow(s, ML + 9.3, y + h / 2, ML + 9.65, y + h / 2);
    node(s, ML + 9.65, y, 2.38, h, "RBAC", "ClusterRole, bound per tenant namespace", { accent: C.green });
    // CN callout under
    s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: ML + 2.95, y: y + h + 0.25, w: 9.08, h: 0.5, fill: { color: cy, transparency: 88 }, line: { color: cy, width: 0.5, transparency: 50 }, rectRadius: 0.08 });
    s.addText([
      { text: "CN = ", options: { color: C.muted } }, { text: "serverless-api.clients.{base_domain}", options: { color: cy, bold: true } }, { text: "   ← this DNS name is the Kubernetes user", options: { color: C.muted } },
    ], { x: ML + 3.1, y: y + h + 0.25, w: 8.9, h: 0.5, fontFace: FONT_M, fontSize: 10.5, margin: 0, valign: "middle", isTextBox: true });
    const cards = [
      ["FiKey", "Identity is a DNS name", "ACME will only issue to a DNS identity, so the certificate's common name is one. RBAC binds exactly that name; the tenant controller has its own."],
      ["FiRepeat", "Server-side apply, always", "Create and update are one code path, with force. A retry after a partial failure simply heals the object; nothing is patched by hand."],
      ["FiMap", "A region is three words", "{name, cluster, registry}. Nothing secret, so regions ship as a ConfigMap and the API server address is derived, never configured."],
      ["FiShield", "Trust is shared", "Both clusters trust the same CA. The OpenShift-injected bundle verifies the servers; connections are lazy, locked and time-boxed."],
    ];
    const cw = (CW - 0.9) / 4;
    for (let k = 0; k < 4; k++) { const [i, t, b] = cards[k]; await card(s, { x: ML + k * (cw + 0.3), y: 4.45, w: cw, h: 2.35, accent: cy, iconName: i, title: t, body: b, bodySize: 11.5, titleSize: 13.5 }); }
  }

  // ---------- 12. Active/active ----------
  {
    const s = base(2, { title: "Active/active: two regions, one truth", sub: "The API runs in both clusters and deploys to both. Traffic follows DNS; the state lives in the Knative Service itself." });
    // DNS on top
    node(s, 4.55, 2.2, 4.2, 0.72, "DNS", "serverless-api.{base_domain}  ·  *.serverless.{base_domain} → the active region", { accent: C.green, subSize: 9 });
    const regions = [["central", ML, cy, "active"], ["south", 6.95, cy, "standby for traffic, live for deploys"]];
    for (const [name, x, a, tag] of regions) {
      const w = 5.73, y = 3.35, h = 2.2;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w, h, fill: { color: C.panel }, line: { color: a, width: 1, dashType: "dash" }, rectRadius: 0.14 });
      s.addText([{ text: name, options: { bold: true, color: C.text, fontSize: 15 } }, { text: "   " + tag, options: { color: C.dim, fontSize: 10, italic: true } }], { x: x + 0.25, y: y + 0.12, w: w - 0.5, h: 0.35, fontFace: FONT_B, margin: 0, isTextBox: true });
      const inner = [["FiCloud", "API"], ["FiUsers", "tenant ctrl"], ["FiTool", "build ctrl"], ["FiPackage", "registry"], ["FiLayers", "workloads"]];
      const iw = (w - 0.5 - 0.4) / 5;
      for (let k = 0; k < 5; k++) {
        const ix = x + 0.25 + k * (iw + 0.1), iy = y + 0.6;
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: ix, y: iy, w: iw, h: 1.35, fill: { color: C.panel2 }, line: { color: C.line, width: 0.75 }, rectRadius: 0.1 });
        s.addImage({ data: await ic(inner[k][0], k === 2 || k === 3 ? am : cy), x: ix + iw / 2 - 0.22, y: iy + 0.22, w: 0.44, h: 0.44 });
        label(s, ix, iy + 0.75, iw, 0.45, inner[k][1], { size: 10.5, color: C.text, bold: true });
      }
      arrow(s, 6.65, 2.92, x + w / 2, 3.35, { color: C.green, width: 1.25 });
    }
    // fan-out arrow between API boxes
    chip(s, ML, 2.95, 3.7, 0.32, "one API call → both regions, concurrently", { accent: cy, size: 9.5, fillAlpha: 84 });
    const facts = [
      ["No leader election, anywhere", "Deterministic names, no timestamps or UUIDs in a spec. Two writers applying the same desired state produce one object and one build."],
      ["A region builds what it runs", "Same commit, different bytes, nothing crosses the boundary at runtime. A switchover needs no reconstruction."],
      ["Partial failure stays honest", "One region failing yields Failed with that region's reason; the healthy region keeps serving. All failing is a 502, and a read that cannot confirm absence is a 503, never a false 404."],
      ["No database", "After a switchover, everything is rebuilt from the KSVC annotations, the runtimes ConfigMap and the persisted git Secret."],
    ];
    const fw = (CW - 0.9) / 4;
    for (let k = 0; k < 4; k++) {
      const x = ML + k * (fw + 0.3), y = 5.75;
      s.addText(facts[k][0], { x, y, w: fw, h: 0.28, fontFace: FONT_B, fontSize: 12, bold: true, color: cy, margin: 0, isTextBox: true });
      s.addText(facts[k][1], { x, y: y + 0.3, w: fw, h: 0.85, fontFace: FONT_B, fontSize: 10, color: C.muted, margin: 0, isTextBox: true, valign: "top" });
    }
  }

  // ---------- 13. Tenant controller ----------
  {
    const s = base(2, { title: "The tenant controller", sub: "A namespace per SSO group, in every region, converged by a separate and more privileged process than the API." });
    await card(s, { x: ML, y: 2.25, w: 3.7, h: 4.2, accent: cy, iconName: "FiLock", title: "Why a separate process", body: [
      "Creating namespaces and writing RBAC is cluster-scoped power the internet-facing API must not hold.",
      "The API may write workloads inside a tenant namespace, but never create a namespace or a NetworkPolicy.",
      "The controller may do exactly that, and may never touch a workload.",
      "Own image, own client certificate, own CN. No auth stack inside: CI fails if pyjwt appears.",
    ], bodySize: 11.5 });
    await card(s, { x: ML + 4.0, y: 2.25, w: 3.85, h: 4.2, accent: cy, iconName: "FiFolder", title: "What lands in {group}-serverless", body: [
      "The Namespace itself, identical in both clusters",
      "Default-deny NetworkPolicies, plus one that admits only kpack build pods",
      "The injected CA-bundle ConfigMap",
      "A RoleBinding for the API's CN, inside this namespace only, and the SCC binding",
      "ExternalSecrets for the region registry and the kpack pull credential",
      "All rendered from template ConfigMaps with {{namespace}} {{group}} {{region}} {{registry}}; a CI job asserts the set is region-neutral.",
    ], bodySize: 11 });
    // three jobs
    const jx = ML + 8.15, jw = 3.88;
    const jobs = [
      ["1", "Provision", "PUT /groups/{group}/namespace before every accepted deploy. Converges every region concurrently and fails closed: an unreachable controller refuses the deploy with 503."],
      ["2", "Reconcile", "Level-triggered, local cluster only so the two sites never fight, every 300 s, with a forced full converge every twelfth pass to repair drift."],
      ["3", "Garbage collect", "Off by default. A namespace empty of workloads for 24 h is deleted, unless serverless.platform/keep says otherwise."],
    ];
    let y = 2.25;
    for (const [n, t, b] of jobs) {
      panel(s, jx, y, jw, 1.3);
      s.addText(n, { x: jx + 0.18, y: y + 0.12, w: 0.5, h: 0.5, fontFace: FONT_T, fontSize: 26, bold: true, color: cy, margin: 0, isTextBox: true });
      s.addText(t, { x: jx + 0.65, y: y + 0.12, w: jw - 0.8, h: 0.4, fontFace: FONT_B, fontSize: 14, bold: true, color: C.text, margin: 0, isTextBox: true, valign: "middle" });
      s.addText(b, { x: jx + 0.18, y: y + 0.5, w: jw - 0.36, h: 0.78, fontFace: FONT_B, fontSize: 10, color: C.muted, margin: 0, isTextBox: true, valign: "top" });
      y += 1.45;
    }
    s.addText([
      { text: "Stamp protocol: ", options: { bold: true, color: cy } },
      { text: "namespace without the hash → contents applied and stale objects pruned → namespace with the template-hash, last. A crash leaves no stamp, so the next pass redoes it.", options: { color: C.muted } },
    ], { x: ML, y: 6.55, w: 10.6, h: 0.45, fontFace: FONT_B, fontSize: 10.5, margin: 0, isTextBox: true, valign: "middle" });
  }

  // ---------- 14. Section: builds ----------
  section(3, "03", "Functions & builds", "A container is 'run this image'. A function is 'here is my repo', and that sentence hides a build.", bgs.secAmber);

  // ---------- 15. Why functions are different ----------
  {
    const s = base(3, { title: "Why functions are different", sub: "A container hands us an image. A function hands us a repository, and the image has to come from somewhere." });
    // two flows
    const fy = 2.35;
    label(s, ML, fy, 3, 0.3, "CONTAINER", { size: 10.5, bold: true, color: cy, align: "left" });
    const cflow = [["image + registry creds", "the request"], ["pull Secret", "per region"], ["Knative Service", "at the given image"], ["Ready", ""]];
    let x = ML; const nw = 2.55;
    cflow.forEach(([t, sub], k) => { node(s, x, fy + 0.4, nw, 0.75, t, sub, { accent: k === 3 ? C.green : cy, titleSize: 12, subSize: 9 }); if (k < 3) arrow(s, x + nw, fy + 0.775, x + nw + 0.3, fy + 0.775); x += nw + 0.3; });
    label(s, ML, fy + 1.4, 3, 0.3, "FUNCTION", { size: 10.5, bold: true, color: am, align: "left" });
    const fflow = [["gitRepo + branch + runtime", "the request"], ["git Secret · build SA · kpack Image", "declared, not run"], ["Knative Service", "at the branch tag"], ["build runs", "minutes, out of band"], ["digest rolled in", "by the build controller"], ["Ready", ""]];
    x = ML; const fw = 1.72;
    fflow.forEach(([t, sub], k) => { node(s, x, fy + 1.8, fw, 0.8, t, sub, { accent: k === 5 ? C.green : am, titleSize: 10.5, subSize: 8.5 }); if (k < 5) arrow(s, x + fw, fy + 2.2, x + fw + 0.34, fy + 2.2); x += fw + 0.34; });
    const cards = [
      ["FiClock", "Asynchronous", "Minutes, not milliseconds. That is why every write returns 202, why Building is a phase of its own, and why the list shows a build state."],
      ["FiRotateCw", "Retried and observable", "Each build phase has its own log, a failure is BuildFailed with a message, and POST /build asks for another go without editing anything."],
      ["FiAlertTriangle", "It fires without anyone asking", "A CVE patch to the base image or a buildpack rebuilds every function on the platform. Nobody clicks. That is the reason we chose this engine."],
    ];
    const cw = (CW - 0.6) / 3;
    for (let k = 0; k < 3; k++) { const [i, t, b] = cards[k]; await card(s, { x: ML + k * (cw + 0.3), y: 5.15, w: cw, h: 1.7, accent: am, iconName: i, title: t, body: b, bodySize: 11 }); }
  }

  // ---------- 16. Buildpacks & kpack ----------
  {
    const s = base(3, { title: "Buildpacks and kpack", sub: "No Dockerfile. Buildpacks detect the language and assemble the image; kpack runs them as Kubernetes objects we can watch." });
    await card(s, { x: ML, y: 2.25, w: 3.85, h: 3.2, accent: am, iconName: "FiBox", title: "Cloud Native Buildpacks", body: [
      "Detect from requirements.txt, go.mod or package.json. Paketo buildpacks, component by component.",
      "Layered images: dependencies, runtime and app are separate layers.",
      "Rebase swaps the run image without a rebuild: a CVE patch in seconds.",
      "Versions are data: BP_CPYTHON_VERSION, BP_GO_VERSION, BP_NODE_VERSION from a runtimes ConfigMap.",
    ], bodySize: 11 });
    await card(s, { x: ML + 4.1, y: 2.25, w: 3.85, h: 3.2, accent: am, iconName: "FiGrid", title: "kpack, in ten CRDs", body: "The ones that matter to us:", bodySize: 11 });
    const crds = ["Image", "Build", "SourceResolver", "ClusterBuilder", "ClusterStack", "ClusterStore", "ClusterLifecycle"];
    let cx = ML + 4.32, cyy = 3.55;
    crds.forEach((c) => { const w = 0.2 + c.length * 0.083; if (cx + w > ML + 4.1 + 3.7) { cx = ML + 4.32; cyy += 0.42; } chip(s, cx, cyy, w, 0.34, c, { accent: am, size: 10, mono: true, fillAlpha: 86, bold: false }); cx += w + 0.1; });
    label(s, ML + 4.32, 5.0, 3.4, 0.4, "Image declares what to build. Build is one run. ClusterBuilder is the toolchain it uses.", { size: 10, italic: true, color: C.dim, align: "left", valign: "top" });
    await card(s, { x: ML + 8.2, y: 2.25, w: 3.83, h: 3.2, accent: am, iconName: "FiGitBranch", title: "Our kpack repo is a chart", body: [
      "Not a fork. A Helm chart that packages upstream kpack 0.18 faithfully and adds what an airgap needs.",
      "CRDs as templates, so the conversion webhook is namespaced correctly.",
      "clusterBuild creates the ClusterStack, ClusterStore and the credentials that pull them.",
      "Mirror scripts pull three artifact classes: images, buildpackages, and the runtimes buildpacks fetch at build time.",
    ], bodySize: 10.5 });
    // topology strip
    panel(s, ML, 5.7, CW, 1.1);
    const ty = 5.92;
    node(s, ML + 0.25, ty, 1.9, 0.66, "ClusterStack", "build + run base images", { accent: am, titleSize: 11.5, subSize: 8.5 });
    node(s, ML + 2.4, ty, 1.9, 0.66, "ClusterStore", "buildpackages", { accent: am, titleSize: 11.5, subSize: 8.5 });
    node(s, ML + 4.55, ty, 1.9, 0.66, "order", "explicit component list", { accent: am, titleSize: 11.5, subSize: 8.5 });
    arrow(s, ML + 6.45, ty + 0.33, ML + 6.95, ty + 0.33, { color: am, width: 1.5 });
    node(s, ML + 6.95, ty, 2.35, 0.66, "ClusterBuilder × 3", "go · python · node", { accent: am, fill: C.panel, titleSize: 11.5, subSize: 8.5 });
    s.addText([
      { text: "Composing a builder is a push. ", options: { bold: true, color: C.text } },
      { text: "The kpack chart owns the read-only, mirror-once content; the serverless chart composes the builders per region, so two clusters never race on one tag.", options: { color: C.muted } },
    ], { x: ML + 9.5, y: 5.8, w: 2.4, h: 0.9, fontFace: FONT_B, fontSize: 9.5, margin: 0, isTextBox: true, valign: "middle" });
  }

  // ---------- 17. Build step by step ----------
  {
    const s = base(3, { title: "A build, step by step", sub: "Image → SourceResolver → Build → Pod. The lifecycle runs as named init containers, so every phase has its own log." });
    // chain
    const chain = [["Image", "declared by the API"], ["SourceResolver", "git ref → commit SHA"], ["Build", "one run, numbered"], ["Pod", "the lifecycle"]];
    let x = ML; const cw = 2.55;
    chain.forEach(([t, sub], k) => { node(s, x, 2.3, cw, 0.7, t, sub, { accent: am, titleSize: 12.5, subSize: 9 }); if (k < 3) arrow(s, x + cw, 2.65, x + cw + 0.3, 2.65); x += cw + 0.3; });
    label(s, x + 0.1, 2.3, 1.2, 0.7, "→ then the phases:", { size: 11, italic: true, color: C.dim, align: "left" });
    // phases
    const phases = [
      ["prepare", "git clone; needs the CA"], ["analyze", "previous image metadata"], ["detect", "run the order; first group wins"], ["restore", "cached layers"],
      ["build", "pip · npm · go via the mirror"], ["export", "assemble layers, push"], ["completion", "the only main container"],
    ];
    const pw = (CW - 6 * 0.18) / 7; x = ML;
    phases.forEach(([t, sub], k) => {
      const isMain = k === 6;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 3.2, w: pw, h: 1.15, fill: { color: isMain ? C.panel : C.panel2 }, line: { color: isMain ? C.green : am, width: 1 }, rectRadius: 0.1, shadow: shadow() });
      s.addText(String(k + 1), { x: x + 0.1, y: 3.25, w: 0.4, h: 0.3, fontFace: FONT_M, fontSize: 9, color: isMain ? C.green : am, margin: 0, isTextBox: true });
      s.addText(t, { x, y: 3.45, w: pw, h: 0.35, fontFace: FONT_M, fontSize: 12.5, bold: true, color: C.text, align: "center", margin: 0, isTextBox: true });
      s.addText(sub, { x: x + 0.08, y: 3.8, w: pw - 0.16, h: 0.5, fontFace: FONT_B, fontSize: 9.5, color: C.muted, align: "center", margin: 0, isTextBox: true, valign: "top" });
      x += pw + 0.18;
    });
    label(s, ML, 4.4, 9, 0.3, "init containers 1–6 run in order; each is a named container, so Cluster.pod_logs(pod, container=…) is a per-phase build log.", { size: 10, italic: true, color: C.dim, align: "left" });
    await card(s, { x: ML, y: 4.72, w: 5.85, h: 2.13, accent: am, iconName: "FiPlay", title: "What starts a build", body: [
      "CONFIG: a PUT changed the spec.  COMMIT: the SourceResolver saw a new SHA.",
      "TRIGGER: POST /build annotates the latest Build, never the Image (that would be a nonce).",
      "BUILDPACK and STACK: a patched buildpackage or run image. These fire with no user action.",
    ], bodySize: 10 });
    await card(s, { x: ML + 6.15, y: 4.72, w: 5.88, h: 2.13, accent: am, iconName: "FiPackage", title: "Where it goes, and how it gets out", body: [
      "{registry}/{org}/{builderRepo}/{group}/{name}:{branch}; the layer cache is a registry tag, not a PVC per function.",
      "A Kyverno ClusterPolicy injects the internal CA, initContainers included: prepare is where the clone happens.",
      "BP_DEPENDENCY_MIRROR → Artifactory with {originalHost}: python.org, nodejs.org and go.dev are different hosts.",
    ], bodySize: 10 });
  }

  // ---------- 18. Build controller ----------
  {
    const s = base(3, { title: "The build controller", sub: "The API declares a build and walks away. The controller watches the result and rolls the digest into the running service." });
    const y = 2.45, h = 1.1;
    node(s, ML, y, 2.9, h, "kpack Image", "status.latestImage changes", { accent: am });
    arrow(s, ML + 2.9, y + h / 2, ML + 3.3, y + h / 2, { color: am, width: 1.5 });
    label(s, ML + 2.8, y - 0.35, 0.6, 0.3, "watch", { size: 9.5, mono: true, color: am });
    node(s, ML + 3.3, y, 2.9, h, "build controller", "label-selected: managed-by=serverless-api, offering=function", { accent: am, subSize: 8.5 });
    arrow(s, ML + 6.2, y + h / 2, ML + 6.6, y + h / 2, { color: am, width: 1.5 });
    label(s, ML + 5.5, y - 0.35, 2.0, 0.3, "server-side apply", { size: 9.5, mono: true, color: am });
    node(s, ML + 6.6, y, 3.0, h, "Knative Service", "image: registry/…@sha256:…", { accent: am, subSize: 9 });
    arrow(s, ML + 9.6, y + h / 2, ML + 10.0, y + h / 2, { color: C.green, width: 1.5 });
    node(s, ML + 10.0, y, 2.03, h, "new Revision", "Knative rolls it out", { accent: C.green });
    label(s, ML, y + h + 0.12, CW, 0.3, "Both ends are local: the Image is here because this region built it, and the digest names this region's registry. Nothing crosses the boundary.", { size: 11, italic: true, color: C.muted, align: "left" });
    const cards = [
      ["FiEdit3", "Exactly one writer per phase", "POST writes the branch tag once per region. PUT keeps each region's own value. POST /build writes no KSVC at all. After that, the controller is the only writer, and only ever a digest."],
      ["FiXOctagon", "It knows when to refuse", "No write when the KSVC already runs that digest, or is not labelled offering: function. The apply is a full SSA of the live object stripped of server-owned metadata."],
      ["FiCpu", "Its own image, no web stack", "Installs cloudlet-apis bare. CI imports the service out of the image and fails if fastapi, uvicorn, jwt or cryptography are inside. What is not installed cannot be flagged."],
      ["FiTrash2", "It cleans up after kpack", "Sweeps kpack's per-build tags in its own registry every 6 h. Deleting a function removes both repositories in every region through the Quay API."],
    ];
    const cw = (CW - 0.9) / 4;
    for (let k = 0; k < 4; k++) { const [i, t, b] = cards[k]; await card(s, { x: ML + k * (cw + 0.3), y: 4.35, w: cw, h: 2.45, accent: am, iconName: i, title: t, body: b, bodySize: 11, titleSize: 13 }); }
  }

  // ---------- 19. Section: portal ----------
  section(4, "04", "Portal & the future", "A console for the whole platform, and a library so the next API starts at step five.", bgs.secViolet);

  // ---------- 20. Portal wiring ----------
  {
    const s = base(4, { title: "How the portal wires it all to the user", sub: "A GCP-style console: Next.js 16 server components in front of the API, with the SSO group playing the part of the project." });
    // diagram left
    const dx = ML, dy = 2.35;
    node(s, dx, dy, 2.1, 0.95, "Browser", "React 19, no client state library", { accent: vi, subSize: 9 });
    arrow(s, dx + 2.1, dy + 0.475, dx + 2.5, dy + 0.475, { color: vi, width: 1.5 });
    node(s, dx + 2.5, dy, 2.3, 0.95, "Portal", "Next.js server actions · Keycloak session", { accent: vi, subSize: 9 });
    arrow(s, dx + 4.8, dy + 0.475, dx + 5.2, dy + 0.475, { color: vi, width: 1.5 });
    node(s, dx + 5.2, dy, 2.1, 0.95, "Serverless API", "/api/serverless/v1", { accent: cy, subSize: 9 });
    label(s, dx + 2.5, dy + 1.0, 2.7, 0.3, "Bearer token forwarded server-side", { size: 9.5, mono: true, color: C.dim, align: "left" });
    // ticket path
    arrow(s, dx + 1.05, dy + 0.95, dx + 1.05, dy + 2.0, { color: C.green, width: 1.25, dash: "dash", head: false });
    arrow(s, dx + 1.05, dy + 2.0, dx + 6.25, dy + 2.0, { color: C.green, width: 1.25, dash: "dash", head: false });
    arrow(s, dx + 6.25, dy + 2.0, dx + 6.25, dy + 0.95, { color: C.green, width: 1.25, dash: "dash" });
    chip(s, dx + 2.2, dy + 1.82, 2.9, 0.36, "SSE with ?ticket= minted server-side", { accent: C.green, size: 9.5, solid: true });
    label(s, dx, dy + 2.35, 7.3, 0.6, "The user's token never reaches the browser. Live logs and stats use a 60-second single-path ticket that the portal re-mints on every reconnect.", { size: 10.5, italic: true, color: C.muted, align: "left", valign: "top" });
    // right rows
    const rows = [
      ["FiUsers", "Groups are the projects", "Membership is re-validated on every request; a forged cookie can never widen access."],
      ["FiEdit", "Create dialog built from /info", "Sizes, regions, runtimes, port bounds, host preview. The portal hardcodes almost nothing."],
      ["FiBell", "202 becomes a tracker toast", "A toast polls /stats every 2.5 s until Ready or Failed; a not-found inside the grace window is the race, not an error."],
      ["FiTerminal", "Seven-tab detail, live logs", "Status per region, Metrics, Variables, Secrets, Files, Advanced, Logs. Streams fall back to polling and say so."],
    ];
    let y = 2.3;
    for (const [i, h, t] of rows) { await iconRow(s, { x: 8.35, y, w: 4.35, accent: vi, iconName: i, head: h, text: t, h: 0.95, textSize: 11 }); y += 1.05; }
    panel(s, ML, 5.75, 7.3, 1.05);
    await badge(s, ML + 0.2, 5.98, 0.55, "FiWifiOff", vi);
    s.addText([
      { text: "Airgap-native by construction. ", options: { bold: true, color: C.text } },
      { text: "No CDN, inline SVG icons, hand-written CSS, an internal npm mirror, a Trivy-gated image and a chart with a default-deny NetworkPolicy.", options: { color: C.muted } },
    ], { x: ML + 0.95, y: 5.8, w: 6.2, h: 0.95, fontFace: FONT_B, fontSize: 11.5, margin: 0, valign: "middle", isTextBox: true });
  }

  // ---------- 21. Portal future ----------
  {
    const s = base(4, { title: "A console, not a Serverless UI", sub: "The service catalog already names the platform we intend to be. Each card lights up the day its API lands." });
    const tiles = [
      ["FiCpu", "Compute"], ["FiZap", "Serverless"], ["FiDatabase", "Databases"], ["FiShare2", "Networking"], ["FiHardDrive", "Object Storage"], ["FiActivity", "Observability"],
      ["FiShuffle", "Data Integration"], ["FiBarChart2", "Data Analytics"], ["FiCode", "Dev Tools"], ["FiTarget", "Machine Learning"], ["FiShield", "Security"], ["FiPlus", "…as data"],
    ];
    const cols = 4, tw = 1.62, th = 1.05, gap = 0.16;
    for (let k = 0; k < tiles.length; k++) {
      const [i, t] = tiles[k]; const col = k % cols, row = Math.floor(k / cols);
      const x = ML + col * (tw + gap), y = 2.3 + row * (th + gap);
      const live = t === "Serverless", next = t === "Object Storage", dots = t === "…as data";
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y, w: tw, h: th, fill: { color: live ? vi : C.panel, transparency: live ? 0 : 0 }, line: { color: live ? vi : next ? vi : C.line, width: live ? 0 : 0.75, dashType: dots ? "dash" : "solid" }, rectRadius: 0.1, shadow: live ? shadow() : undefined });
      s.addImage({ data: await ic(i, live ? C.bg : next ? vi : "5B6B8A"), x: x + 0.16, y: y + 0.16, w: 0.34, h: 0.34 });
      s.addText(t, { x: x + 0.14, y: y + 0.55, w: tw - 0.28, h: 0.3, fontFace: FONT_B, fontSize: 11, bold: true, color: live ? C.bg : next ? C.text : C.muted, margin: 0, isTextBox: true });
      if (live) s.addText("LIVE", { x: x + tw - 0.6, y: y + 0.14, w: 0.5, h: 0.22, fontFace: FONT_M, fontSize: 8, bold: true, color: C.bg, align: "right", margin: 0, isTextBox: true });
      if (next) s.addText("NEXT", { x: x + tw - 0.6, y: y + 0.14, w: 0.5, h: 0.22, fontFace: FONT_M, fontSize: 8, bold: true, color: vi, align: "right", margin: 0, isTextBox: true });
    }
    label(s, ML, 5.95, 7.0, 0.7, "Object Storage already has its hook: PORTAL_STORAGE_API_URL. Set it, and the card is a product.", { size: 11, italic: true, color: C.muted, align: "left", valign: "top" });
    const rows = [
      ["FiSliders", "An offering is data, not code", "PORTAL_SERVICES JSON or PORTAL_<NAME>_API_URL adds a card, a route and nav entries. Unknown ids are appended."],
      ["FiLayout", "The shell is already generic", "TopBar, SideNav, auth, the group cookie, theme, icons. Only the serverless tree and its client are product-specific."],
      ["FiCopy", "The reusable shape is legible", "A lib/<id>.ts client, an actions.ts, one context gate per product, and forms driven by that API's /info."],
      ["FiArrowUpRight", "What we do next", "Lift a shared resource layer out of the serverless tree, and generate the TypeScript client from /openapi.json so the contract stops being copied by hand."],
    ];
    let y = 2.3;
    for (const [i, h, t] of rows) { await iconRow(s, { x: 8.15, y, w: 4.55, accent: vi, iconName: i, head: h, text: t, h: 1.1, textSize: 10.5 }); y += 1.15; }
  }

  // ---------- 22. cloudlet-apis ----------
  {
    const s = base(4, { title: "cloudlet-apis: the next API starts at step five", sub: "Everything we got right building the Serverless API, published as a library instead of copied into the next repo." });
    // layer stack left
    const lx = ML, lw = 5.4;
    const layers = [
      ["[auth]", "OIDC discovery + JWKS cache · admin key on the same header · SSO login in Swagger · token proxy · stream tickets", vi, C.panel2, "adds FastAPI, httpx, pyjwt"],
      ["[web]", "health probes · one error envelope · offline Swagger and ReDoc · base_path for a shared host", vi, C.panel, "adds FastAPI"],
      ["core", "APIError catalogue · Name and Group rules · normalize_group · logging · X-Request-ID", vi, C.panel, "pydantic only"],
    ];
    let y = 2.3;
    for (const [t, d, a, f, extra] of layers) {
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: lx, y, w: lw, h: 1.15, fill: { color: f }, line: { color: a, width: 1 }, rectRadius: 0.1, shadow: shadow() });
      s.addText(t, { x: lx + 0.2, y: y + 0.12, w: 1.3, h: 0.4, fontFace: FONT_M, fontSize: 14, bold: true, color: a, margin: 0, isTextBox: true });
      s.addText(extra, { x: lx + 1.5, y: y + 0.14, w: lw - 1.7, h: 0.35, fontFace: FONT_M, fontSize: 9, color: C.dim, margin: 0, isTextBox: true, align: "right" });
      s.addText(d, { x: lx + 0.2, y: y + 0.5, w: lw - 0.4, h: 0.6, fontFace: FONT_B, fontSize: 11, color: C.muted, margin: 0, isTextBox: true, valign: "top" });
      y += 1.27;
    }
    label(s, lx, y + 0.02, lw, 0.6, "Enforced, not documented: a test spawns a fresh interpreter per core module and asserts FastAPI never appears. The build controller ships with no web stack because of it.", { size: 10, italic: true, color: C.dim, align: "left", valign: "top" });
    // right: wiring steps
    const rx = 6.5, rw = 6.18;
    panel(s, rx, 2.3, rw, 3.3);
    s.addText("WIRING A NEW API", { x: rx + 0.25, y: 2.42, w: 3, h: 0.3, fontFace: FONT_B, fontSize: 10.5, bold: true, color: vi, charSpacing: 3, margin: 0, isTextBox: true });
    const steps = [
      ["Settings", "your BaseSettings embeds SSOConfig; the library never reads the environment"],
      ["Auth", "SSOAuth(settings.sso) and a CurrentUser dependency"],
      ["App", "logging, RequestIDMiddleware, exception handlers, offline docs, SSO login, health router"],
      ["Errors", "subclass APIError; the catalogue publishes itself on /info"],
      ["Streams", "StreamTickets, only if you serve SSE"],
    ];
    let sy = 2.8;
    steps.forEach(([t, d], k) => {
      s.addText(String(k + 1), { x: rx + 0.25, y: sy, w: 0.4, h: 0.48, fontFace: FONT_T, fontSize: 20, bold: true, color: vi, margin: 0, isTextBox: true });
      s.addText([{ text: t + "  ", options: { bold: true, color: C.text } }, { text: d, options: { color: C.muted } }], { x: rx + 0.7, y: sy, w: rw - 0.95, h: 0.48, fontFace: FONT_B, fontSize: 11.5, margin: 0, isTextBox: true, valign: "middle" });
      sy += 0.46;
    });
    s.addText("≈ 15 lines of wiring, plus the security review that came with them: constant-time compares, a grant whitelist, body caps, uniform 401s, discovery backoff.", { x: rx + 0.25, y: sy + 0.02, w: rw - 0.5, h: 0.36, fontFace: FONT_B, fontSize: 10, italic: true, color: C.dim, margin: 0, isTextBox: true, valign: "top" });
    panel(s, rx, 5.8, rw, 1.0);
    await badge(s, rx + 0.2, 6.05, 0.5, "FiAlertCircle", vi);
    s.addText([
      { text: "Why it matters. ", options: { bold: true, color: C.text } },
      { text: "Two APIs normalizing groups differently is an authorization bug, not a formatting one. Next to extract: the mTLS multi-cluster client and the region fan-out.", options: { color: C.muted } },
    ], { x: rx + 0.85, y: 5.85, w: rw - 1.05, h: 0.9, fontFace: FONT_B, fontSize: 11, margin: 0, valign: "middle", isTextBox: true });
  }

  // ---------- 23. Closing ----------
  {
    const s = pres.addSlide(); slideNo += 1;
    s.background = { data: bgs.title };
    s.addText("WHAT WE BUILT", { x: ML, y: 0.9, w: 8, h: 0.35, fontFace: FONT_B, fontSize: 12, bold: true, color: cy, charSpacing: 4, margin: 0, isTextBox: true });
    s.addText("Code in, a running service in two regions out.", { x: ML, y: 1.3, w: 11.5, h: 1.0, fontFace: FONT_T, fontSize: 40, bold: true, color: C.text, margin: 0, isTextBox: true, valign: "middle" });
    const stats = [["4", "repositories", cy], ["3", "services, 3 images", cy], ["23", "API endpoints", cy], ["2", "regions, active/active", cy], ["3", "runtimes: go · python · node", am], ["0", "kubectl for customers", vi]];
    const sw = (CW - 5 * 0.25) / 6;
    stats.forEach(([b, t, a], k) => stat(s, ML + k * (sw + 0.25), 2.75, sw, b, t, a));
    const takeaways = [
      "The operator owns Knative; we own the contract on top of it.",
      "The spec is a pure function of the definition, so two regions need no leader.",
      "Builds are declared, watched, and rolled in by a controller that can only write digests.",
      "The portal and the library exist so the second product is cheaper than the first.",
    ];
    const items = takeaways.map((t, i) => ({ text: t, options: { bullet: { indent: 14 }, breakLine: i < takeaways.length - 1, paraSpaceAfter: 8 } }));
    s.addText(items, { x: ML, y: 4.6, w: 8.6, h: 1.9, fontFace: FONT_B, fontSize: 15, color: C.muted, margin: 0, valign: "top", isTextBox: true });
    s.addText("Thank you. Questions?", { x: 9.4, y: 5.6, w: 3.3, h: 0.6, fontFace: FONT_T, fontSize: 22, bold: true, color: C.text, margin: 0, isTextBox: true, align: "right", valign: "middle" });
    s.addText(`${String(slideNo).padStart(2, "0")} / ${TOTAL}`, { x: W - 1.7, y: H - 0.55, w: 1.05, h: 0.3, fontFace: FONT_M, fontSize: 9.5, color: C.dim, align: "right", margin: 0, isTextBox: true });
  }

  if (slideNo !== TOTAL) console.warn("slide count mismatch", slideNo, TOTAL);
  pres.slides.forEach((sl, i) => { if (NOTES[i]) sl.addNotes(NOTES[i]); });
  await pres.writeFile({ fileName: __dirname + "/serverless-platform.pptx" });
  console.log("written", slideNo, "slides");
}
build().catch((e) => { console.error(e); process.exit(1); });
