// PPTX renderer for the light design. Every animated element carries objectName "step:N";
// animate.js turns those into click-to-reveal fade entrances.
const pptxgen = require("pptxgenjs");
const { slides } = require("./content");
const { anchors } = require("./geom");
const LOGOS = require("./logos.json");

const C = { paper: "FFFFFF", ink: "1B1F2A", muted: "667085", hair: "E4E7EC", panel: "F4F5F7", accent: "E4572E", teal: "0B7A75", tealSoft: "E3F1F0", ok: "1B8A5A", bad: "C0392B", edge: "98A2B3" };
const FD = "Century Schoolbook", FB = "Calibri", FM = "Courier New";
const W = 13.333, H = 7.5, ML = 0.7;
const TOTAL = slides.length;
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
pres.title = "From Knative to the Portal";

const step = (n, eff = "wipeL") => ({ objectName: `step:${n}:${eff}` });
// While fn runs, every shape/text added to the slide is tagged auto:N:<effect> so animate.js
// plays it automatically when the slide opens, staggered in document order.
function autoWrap(s, fn) {
  const origShape = s.addShape.bind(s), origText = s.addText.bind(s);
  let n = 0;
  s.addShape = (type, o) => origShape(type, Object.assign({ objectName: `auto:${n++}:${type === pres.shapes.LINE ? "wipeL" : "wipeD"}` }, o));
  s.addText = (t, o) => origText(t, Object.assign({ objectName: `auto:${n++}:${(o && o.fontSize || 0) >= 40 ? "zoom" : "fade"}` }, o));
  try { fn(); } finally { s.addShape = origShape; s.addText = origText; }
}
function txt(s, text, o) { s.addText(text, Object.assign({ fontFace: FB, color: C.ink, margin: 0, isTextBox: true }, o)); }
function foot(s, i) { txt(s, `${String(i + 1).padStart(2, "0")} / ${TOTAL}`, { x: ML, y: H - 0.65, w: 2, h: 0.3, fontFace: FM, fontSize: 10, color: C.muted }); }
function kicker(s, t, color) { txt(s, t.toUpperCase(), { x: ML, y: 0.55, w: 10, h: 0.3, fontFace: FM, fontSize: 11, color: color || C.accent, charSpacing: 3, bold: true }); }
function title(s, t, o) { txt(s, t, Object.assign({ x: ML, y: 0.9, w: 11.5, h: 1.0, fontFace: FD, fontSize: 38, bold: true, valign: "middle" }, o)); }

// visual box: 600x440 units → 5.9 x 4.33 in at (6.75, 2.25)
const VX = 6.75, VY = 2.2, K = 5.9 / 600;
let gx = VX, gy = VY, gk = K;
const vx = (u) => gx + u * gk, vy = (u) => gy + u * gk, vs = (u) => u * gk;

function mkNm(ctx) {
  let auto = 0;
  return (k, eff) => {
    if (!ctx || !ctx.reveal || ctx.reveal === "auto") return { objectName: `auto:${auto++}:${eff}` };
    const n = ctx.nLines;
    const st = ctx.reveal === "paired" ? k + 1 : ctx.reveal === "rows" ? n + k + 1 : n + 1;
    return { objectName: `step:${st}:${eff}` };
  };
}
function node(s, n, nm, k) {
  const acc = n.tone === "accent";
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x: vx(n.x), y: vy(n.y), w: vs(n.w), h: vs(n.h), fill: { color: C.paper }, line: { color: acc ? C.accent : C.teal, width: acc ? 1.75 : 1.25 }, rectRadius: 0.08 }, nm(k, "wipeD")));
  const big = gk > K;
  const runs = [{ text: n.label, options: { fontSize: big ? 13 : 12, bold: true, color: C.ink, breakLine: !!n.sub } }];
  if (n.sub) runs.push({ text: n.sub, options: { fontSize: big ? 9.5 : 8.5, color: C.muted, fontFace: FM } });
  s.addText(runs, Object.assign({ x: vx(n.x), y: vy(n.y), w: vs(n.w), h: vs(n.h), fontFace: FB, align: "center", valign: "middle", margin: 0.04, isTextBox: true }, nm(k, "wipeD")));
}
function edge(s, a, b, dashed, nm, k) {
  const p = anchors(a, b);
  const x1 = vx(p.x1), y1 = vy(p.y1), x2 = vx(p.x2), y2 = vy(p.y2);
  const o = { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1), line: { color: C.edge, width: 1.25, endArrowType: "triangle", dashType: dashed ? "dash" : "solid" } };
  if (x2 < x1) o.flipH = true;
  if (y2 < y1) o.flipV = true;
  s.addShape(pres.shapes.LINE, Object.assign(o, nm(k, "wipeL")));
}
function visual(s, v, ctx) {
  if (!v) return;
  const nm = mkNm(ctx);
  const wide = !!(ctx && ctx.wide);
  switch (v.kind) {
    case "glyph":
      txt(s, v.text, Object.assign({ x: VX, y: VY + 0.9, w: 5.9, h: 1.4, fontFace: FD, fontSize: v.text.length > 12 ? 30 : 44, bold: true, color: C.teal, align: "center", valign: "middle" }, nm(0, "zoom")));
      txt(s, v.sub, Object.assign({ x: VX, y: VY + 2.3, w: 5.9, h: 0.4, fontFace: FM, fontSize: 11, color: C.muted, align: "center" }, nm(0, "fade")));
      break;
    case "graph": {
      const bw = (v.box && v.box[0]) || 600, bh = (v.box && v.box[1]) || 440;
      if (wide) { gk = Math.min(4.5 / bh, 11.9 / bw); gx = (W - bw * gk) / 2; gy = 2.3; }
      else { gk = 5.9 / bw; gy = VY; gx = VX; }
      const byId = Object.fromEntries(v.nodes.map((n) => [n.id, n]));
      const idx = Object.fromEntries(v.nodes.map((n, i) => [n.id, i]));
      v.edges.forEach((e) => edge(s, byId[e.from], byId[e.to], e.dashed, nm, Math.max(idx[e.from], idx[e.to])));
      v.nodes.forEach((n, i) => node(s, n, nm, i));
      gx = VX; gy = VY; gk = K;
      break;
    }
    case "stack": {
      const n = v.layers.length, gap = 0.12, h = Math.min(0.78, (4.33 - gap * (n - 1)) / n);
      v.layers.forEach(([t, sub, tone], i) => {
        const y = VY + i * (h + gap), acc = tone === "accent";
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x: VX, y, w: 5.9, h, fill: { color: C.paper }, line: { color: acc ? C.accent : C.teal, width: acc ? 1.75 : 1.25 }, rectRadius: 0.08 }, nm(i, "wipeD")));
        txt(s, t, Object.assign({ x: VX + 0.2, y, w: 2.6, h, fontSize: 14, bold: true, valign: "middle" }, nm(i, "wipeD")));
        txt(s, sub, Object.assign({ x: VX + 2.6, y, w: 3.1, h, fontFace: FM, fontSize: 9.5, color: C.muted, align: "right", valign: "middle" }, nm(i, "wipeD")));
      });
      break;
    }
    case "table": {
      const cols = wide ? [2.6, 4.6, 4.7] : [1.3, 2.2, 2.4], rh = wide ? Math.min(0.78, 4.0 / v.rows.length) : 0.55;
      const X0 = wide ? ML : VX, TW = wide ? 11.9 : 5.9, FS = wide ? 17 : 12.5;
      let y = wide ? 2.3 : VY + 0.2;
      const xs = [X0, X0 + cols[0], X0 + cols[0] + cols[1]];
      v.head.forEach((h, i) => { if (h) txt(s, h.toUpperCase(), Object.assign({ x: xs[i], y, w: cols[i], h: 0.3, fontFace: FM, fontSize: wide ? 11 : 9, charSpacing: 2, color: i === 2 && !v.plain ? C.teal : C.muted }, nm(0, "fade"))); });
      y += wide ? 0.55 : 0.4;
      v.rows.forEach((r, k) => {
        if (!v.plain) s.addShape(pres.shapes.RECTANGLE, Object.assign({ x: xs[2] - 0.1, y, w: cols[2] + 0.1, h: rh, fill: { color: C.tealSoft }, line: { color: C.tealSoft, width: 0 } }, nm(k, "fade")));
        r.forEach((c, i) => txt(s, c, Object.assign({ x: xs[i] + (i ? 0.05 : 0), y, w: cols[i] - 0.1, h: rh, fontSize: FS, bold: i === 0, color: i === 1 && !v.plain ? C.muted : C.ink, valign: "middle" }, nm(k, "wipeL"))));
        if (k) s.addShape(pres.shapes.LINE, Object.assign({ x: X0, y, w: TW, h: 0, line: { color: C.hair, width: 0.5 } }, nm(k, "wipeL")));
        y += rh;
      });
      break;
    }
    case "stats": {
      v.items.forEach(([b, t], i) => {
        const x = VX + (i % 2) * 3.0, y = VY + 0.2 + Math.floor(i / 2) * 2.0;
        txt(s, b, Object.assign({ x, y, w: 2.8, h: 1.1, fontFace: FD, fontSize: 54, bold: true, color: C.accent, valign: "bottom" }, nm(i, "zoom")));
        txt(s, t, Object.assign({ x, y: y + 1.12, w: 2.8, h: 0.35, fontFace: FM, fontSize: 10.5, color: C.muted }, nm(i, "fade")));
      });
      break;
    }
    case "code": {
      const X0 = wide ? ML : VX, W2 = wide ? 11.9 : 5.9;
      const Y0 = wide ? 2.25 : VY + 0.2, HMAX = wide ? 4.55 : 3.6;
      const mult = wide ? 1.18 : 1.35;
      // fit the block to the panel: rendered line height is about fontSize * 1.2 * mult
      const FS = Math.min(wide ? 12 : 11.5, Math.floor(((HMAX - 0.45) * 72) / (v.lines.length * 1.2 * mult) * 2) / 2);
      // and then pull the panel in around a short block
      const H2 = wide ? Math.min(HMAX, (v.lines.length * FS * 1.2 * mult) / 72 + 0.5) : HMAX;
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x: X0, y: Y0, w: W2, h: H2, fill: { color: C.panel }, line: { color: C.panel, width: 0 }, rectRadius: 0.1 }, nm(0, "fade")));
      const runs = v.lines.map(([t, tone], i) => ({ text: t || " ", options: { color: tone === "accent" ? C.accent : tone === "ink" ? C.ink : C.muted, bold: tone === "accent", breakLine: i < v.lines.length - 1 } }));
      s.addText(runs, Object.assign({ x: X0 + 0.25, y: Y0 + 0.2, w: W2 - 0.5, h: H2 - 0.4, fontFace: FM, fontSize: FS, margin: 0, valign: "top", isTextBox: true, lineSpacingMultiple: mult }, nm(0, "fade")));
      break;
    }
    case "lifecycle": {
      let x = VX; const y = VY + 0.3, nP = v.phases.length;
      v.phases.forEach((p, i) => {
        const ok = p === "Ready", w = 1.15;
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x, y, w, h: 0.42, fill: { color: ok ? C.ok : C.paper }, line: { color: ok ? C.ok : C.teal, width: 1.25 }, rectRadius: 0.06 }, nm(i, "wipeL")));
        txt(s, p, Object.assign({ x, y, w, h: 0.42, fontSize: 12, bold: true, color: ok ? "FFFFFF" : C.teal, align: "center", valign: "middle" }, nm(i, "wipeL")));
        if (i < nP - 1) s.addShape(pres.shapes.LINE, Object.assign({ x: x + w, y: y + 0.21, w: 0.28, h: 0, line: { color: C.edge, width: 1.25, endArrowType: "triangle" } }, nm(i + 1, "wipeL")));
        x += w + 0.28;
      });
      s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x: VX, y: y + 0.85, w: 1.15, h: 0.42, fill: { color: C.paper }, line: { color: C.bad, width: 1.25 }, rectRadius: 0.06 }, nm(nP, "wipeL")));
      txt(s, "Failed", Object.assign({ x: VX, y: y + 0.85, w: 1.15, h: 0.42, fontSize: 12, bold: true, color: C.bad, align: "center", valign: "middle" }, nm(nP, "wipeL")));
      txt(s, "with a reason:", Object.assign({ x: VX + 1.3, y: y + 0.85, w: 3, h: 0.42, fontSize: 12, color: C.muted, valign: "middle" }, nm(nP, "wipeL")));
      let rx = VX, ry = y + 1.5;
      v.failed.forEach((r) => {
        const w = 0.2 + r.length * 0.082;
        if (rx + w > VX + 5.9) { rx = VX; ry += 0.44; }
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x: rx, y: ry, w, h: 0.34, fill: { color: C.paper }, line: { color: C.hair, width: 0.75 }, rectRadius: 0.05 }, nm(nP + 1, "fade")));
        txt(s, r, Object.assign({ x: rx, y: ry, w, h: 0.34, fontFace: FM, fontSize: 9.5, color: C.bad, align: "center", valign: "middle" }, nm(nP + 1, "fade")));
        rx += w + 0.12;
      });
      break;
    }
    case "phases": {
      const cols = 4, gw = 1.36, gh = 0.95, g = 0.15;
      v.items.forEach(([t, sub], i) => {
        const x = VX + (i % cols) * (gw + g), y = VY + 0.3 + Math.floor(i / cols) * (gh + g), ok = i === v.items.length - 1;
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x, y, w: gw, h: gh, fill: { color: C.paper }, line: { color: ok ? C.ok : C.teal, width: 1.25 }, rectRadius: 0.08 }, nm(i, "wipeD")));
        txt(s, String(i + 1), Object.assign({ x: x + gw - 0.4, y: y + 0.08, w: 0.3, h: 0.25, fontFace: FM, fontSize: 8.5, color: C.muted, align: "right" }, nm(i, "wipeD")));
        txt(s, t, Object.assign({ x: x + 0.12, y: y + 0.18, w: gw - 0.24, h: 0.35, fontFace: FM, fontSize: 12, bold: true }, nm(i, "wipeD")));
        txt(s, sub, Object.assign({ x: x + 0.12, y: y + 0.52, w: gw - 0.24, h: 0.4, fontSize: 9, color: C.muted }, nm(i, "wipeD")));
      });
      break;
    }
    case "tiles": {
      const cols = 4, gw = 1.38, gh = 0.72, g = 0.12;
      const nx = Array.isArray(v.next) ? v.next : [v.next];
      v.items.forEach((t, i) => {
        const x = VX + (i % cols) * (gw + g), y = VY + 0.3 + Math.floor(i / cols) * (gh + g);
        const live = t === v.live, next = nx.includes(t);
        s.addShape(pres.shapes.ROUNDED_RECTANGLE, Object.assign({ x, y, w: gw, h: gh, fill: { color: live ? C.accent : next ? C.paper : C.panel }, line: { color: live || next ? C.accent : C.hair, width: 1 }, rectRadius: 0.08 }, nm(i, "wipeD")));
        txt(s, t, Object.assign({ x: x + 0.12, y, w: gw - 0.24, h: gh, fontSize: 10.5, bold: true, color: live ? "FFFFFF" : next ? C.ink : C.muted, valign: "middle" }, nm(i, "wipeD")));
        if (live || next) txt(s, live ? "LIVE" : "NEXT", Object.assign({ x: x + gw - 0.6, y: y + 0.06, w: 0.5, h: 0.2, fontFace: FM, fontSize: 7.5, bold: true, color: live ? "FFFFFF" : C.accent, align: "right" }, nm(i, "wipeD")));
      });
      break;
    }
  }
}

slides.forEach((sd, i) => {
  const s = pres.addSlide();
  s.background = { color: sd.kind === "section" ? C.accent : C.paper };
  if (sd.notes) s.addNotes(sd.notes);
  if (sd.kind === "title") {
    autoWrap(s, () => {
      txt(s, sd.kicker.toUpperCase(), { x: ML, y: 2.0, w: 8, h: 0.35, fontFace: FM, fontSize: 12, color: C.accent, charSpacing: 4, bold: true });
      txt(s, sd.title, { x: ML, y: 2.4, w: 8.4, h: 2.0, fontFace: FD, fontSize: 66, bold: true, valign: "middle", lineSpacingMultiple: 0.95 });
      txt(s, sd.sub, { x: ML, y: 4.5, w: 8, h: 0.6, fontFace: FD, fontSize: 24, italic: true, color: C.muted });
      txt(s, sd.meta, { x: ML, y: 5.4, w: 6, h: 0.35, fontFace: FM, fontSize: 11, color: C.muted });
      [1.0, 1.7, 2.4].forEach((d, k) => s.addShape(pres.shapes.OVAL, { x: 11.0 - d / 2, y: 3.75 - d / 2, w: d, h: d, fill: { color: C.paper, transparency: 100 }, line: { color: C.accent, width: 1.5, transparency: 80 - k * 25 } }));
    });
    return;
  }
  if (sd.kind === "section") {
    if (sd.num) txt(s, sd.num, { x: ML, y: 1.0, w: 6, h: 2.6, fontFace: FD, fontSize: 150, bold: true, color: "FFFFFF", valign: "middle" });
    txt(s, sd.title, Object.assign({ x: ML, y: sd.num ? 3.7 : 2.4, w: 11.5, h: sd.num ? 1.0 : 1.6, fontFace: FD, fontSize: sd.num ? 48 : 64, bold: true, color: "FFFFFF", valign: "middle" }, { objectName: "auto:0:wipeL" }));
    txt(s, sd.sub, Object.assign({ x: ML, y: sd.num ? 4.75 : 4.1, w: 10, h: 0.6, fontFace: FD, fontSize: 22, italic: true, color: "FFFFFF" }, { objectName: "auto:1:fade" }));
    txt(s, `${String(i + 1).padStart(2, "0")} / ${TOTAL}`, { x: ML, y: H - 0.65, w: 2, h: 0.3, fontFace: FM, fontSize: 10, color: "FFFFFF" });
    return;
  }
  if (sd.kind === "closing") {
    kicker(s, sd.kicker); title(s, sd.title, { fontSize: 42 });
    const sw = (W - 2 * ML - 5 * 0.25) / 6;
    sd.stats.forEach(([b, t], k) => {
      const x = ML + k * (sw + 0.25);
      txt(s, b, Object.assign({ x, y: 2.2, w: sw, h: 1.0, fontFace: FD, fontSize: 48, bold: true, color: C.accent, valign: "bottom" }, step(k + 1, "zoom")));
      txt(s, t, Object.assign({ x, y: 3.22, w: sw, h: 0.35, fontFace: FM, fontSize: 10.5, color: C.muted }, step(k + 1, "fade")));
    });
    sd.lines.forEach((l, k) => {
      const y = 4.1 + k * 0.55, n = sd.stats.length + k + 1;
      s.addShape(pres.shapes.RECTANGLE, Object.assign({ x: ML, y: y + 0.19, w: 0.12, h: 0.12, fill: { color: C.accent }, line: { color: C.accent, width: 0 } }, step(n)));
      txt(s, l, Object.assign({ x: ML + 0.32, y, w: 8.5, h: 0.5, fontSize: 18, valign: "middle" }, step(n)));
    });
    txt(s, "Thank you. Questions?", Object.assign({ x: 8.5, y: 5.8, w: 4.1, h: 0.7, fontFace: FD, fontSize: 26, bold: true, align: "right", valign: "middle" }, step(sd.stats.length + sd.lines.length + 1, "zoom")));
    foot(s, i);
    return;
  }
  kicker(s, sd.kicker); title(s, sd.title);
  if (sd.logo && LOGOS[sd.logo]) {
    const g = LOGOS[sd.logo], lh2 = 0.95, lw = lh2 * (g.w / g.h);
    s.addImage({ data: g.data, x: W - ML - lw, y: 0.5, w: lw, h: lh2, objectName: "auto:99:fade" });
  }
  const n = sd.lines.length, lede = sd.textSize === "lede", small = sd.textSize === "small";
  const lh = n ? Math.min(lede ? 1.35 : 0.72, 4.3 / n) : 0;
  sd.lines.forEach((l, k) => {
    const y = 2.3 + k * lh;
    s.addShape(pres.shapes.RECTANGLE, Object.assign({ x: ML, y: lede ? y + 0.22 : y + lh / 2 - 0.07, w: lede ? 0.16 : 0.13, h: lede ? 0.16 : 0.13, fill: { color: C.accent }, line: { color: C.accent, width: 0 } }, step(k + 1)));
    txt(s, l, Object.assign({ x: ML + (lede ? 0.45 : 0.35), y, w: lede ? 5.35 : 5.45, h: lh, fontSize: lede ? 24 : small ? 15 : n > 5 ? 17 : 19, valign: lede ? "top" : "middle", lineSpacingMultiple: lede ? 1.12 : 1.0, bold: false }, step(k + 1)));
  });
  visual(s, sd.visual, { reveal: sd.reveal || "auto", nLines: n, wide: !!(sd.wide || !n) });
  foot(s, i);
});

pres.writeFile({ fileName: __dirname + "/deck-raw.pptx" }).then(() => console.log("written", slides.length, "slides"));
