const fs = require("fs");
const { slides } = require("./content");
const { anchors } = require("./geom");

const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const TOTAL = slides.length;

function graphSvg(v) {
  const byId = Object.fromEntries(v.nodes.map((n) => [n.id, n]));
  let out = `<svg class="graph" viewBox="0 0 600 440" role="img" aria-label="diagram">
  <defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="var(--edge)"/></marker></defs>`;
  v.edges.forEach((e, i) => {
    const p = anchors(byId[e.from], byId[e.to]);
    out += `<line class="edge build-auto${e.dashed ? "" : " draw"}" style="--d:${(i + v.nodes.length) * 70}ms" x1="${p.x1}" y1="${p.y1}" x2="${p.x2}" y2="${p.y2}" ${e.dashed ? 'stroke-dasharray="6 6"' : 'pathLength="1"'} marker-end="url(#ah)"/>`;
  });
  v.nodes.forEach((n, i) => {
    const cx = n.x + n.w / 2, cy = n.y + n.h / 2;
    out += `<g class="node ${n.tone} build-auto" style="--d:${i * 70}ms">
      <rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="8"/>
      <text x="${cx}" y="${n.sub ? cy - 4 : cy + 5}" class="nl">${esc(n.label)}</text>
      ${n.sub ? `<text x="${cx}" y="${cy + 15}" class="ns">${esc(n.sub)}</text>` : ""}
    </g>`;
  });
  return out + "</svg>";
}
function visual(v) {
  if (!v) return "";
  switch (v.kind) {
    case "glyph": return `<div class="glyph build-auto"><div class="g">${esc(v.text)}</div><div class="gs">${esc(v.sub)}</div></div>`;
    case "graph": return graphSvg(v);
    case "stack": return `<div class="stack">${v.layers.map(([t, s, tone], i) => `<div class="layer ${tone || ""} build-auto" style="--d:${i * 80}ms"><b>${esc(t)}</b><span>${esc(s)}</span></div>`).join("")}</div>`;
    case "table": return `<table class="cmp build-auto"><thead><tr>${v.head.map((h, i) => `<th class="${i === 2 && !v.plain ? "acc" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${v.rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i === 0 ? "k" : i === 2 && !v.plain ? "acc" : v.plain ? "pl" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    case "stats": return `<div class="stats">${v.items.map(([b, s], i) => `<div class="stat build-auto" style="--d:${i * 90}ms"><div class="big" data-n="${esc(b)}">${esc(b)}</div><div class="sm">${esc(s)}</div></div>`).join("")}</div>`;
    case "code": return `<pre class="code build-auto">${v.lines.map(([t, tone]) => `<span class="${tone}">${esc(t)}</span>`).join("\n")}</pre>`;
    case "lifecycle": return `<div class="life"><div class="row">${v.phases.map((p, i) => `<span class="chip ${p === "Ready" ? "ok" : ""} build-auto" style="--d:${i * 90}ms">${esc(p)}</span>${i < v.phases.length - 1 ? '<span class="arr">→</span>' : ""}`).join("")}</div><div class="row failed build-auto" style="--d:400ms"><span class="chip bad">Failed</span><span class="why">with a reason:</span></div><div class="row reasons build-auto" style="--d:480ms">${v.failed.map((r) => `<code>${esc(r)}</code>`).join("")}</div></div>`;
    case "phases": return `<ol class="phases">${v.items.map(([t, s], i) => `<li class="build-auto ${i === 6 ? "ok" : ""}" style="--d:${i * 70}ms"><code>${esc(t)}</code><span>${esc(s)}</span></li>`).join("")}</ol>`;
    case "tiles": return `<div class="tiles">${v.items.map((t, i) => `<div class="tile ${t === v.live ? "live" : t === v.next ? "next" : ""} build-auto" style="--d:${i * 40}ms"><span>${esc(t)}</span>${t === v.live ? "<em>live</em>" : t === v.next ? "<em>next</em>" : ""}</div>`).join("")}</div>`;
    default: return "";
  }
}
function slideHtml(s, i) {
  const num = String(i + 1).padStart(2, "0");
  const notes = s.notes ? `<div class="notes" hidden>${esc(s.notes)}</div>` : "";
  if (s.kind === "title") {
    return `<section class="slide title" data-i="${i}">
      <div class="kicker build" data-step="0">${esc(s.kicker)}</div>
      <h1 class="build" data-step="0">${s.title.split(" ").map((w, k) => `<span class="w" style="--i:${k}">${esc(w)}</span>`).join(" ")}</h1>
      <p class="sub build" data-step="0">${esc(s.sub)}</p>
      <p class="meta build" data-step="0">${esc(s.meta)}</p>
      <div class="pulse" aria-hidden="true"><span></span><span></span><span></span></div>${notes}</section>`;
  }
  if (s.kind === "section") {
    return `<section class="slide section" data-i="${i}">
      ${s.num ? `<div class="num build" data-step="0">${esc(s.num)}</div>` : ""}
      <h1 class="build" data-step="0">${esc(s.title)}</h1>
      <p class="sub build" data-step="0">${esc(s.sub)}</p>${notes}</section>`;
  }
  if (s.kind === "closing") {
    return `<section class="slide closing" data-i="${i}">
      <div class="kicker">${esc(s.kicker)}</div>
      <h1>${esc(s.title)}</h1>
      <div class="stats wide">${s.stats.map(([b, t], k) => `<div class="stat build" data-step="${k + 1}"><div class="big" data-n="${esc(b)}">${esc(b)}</div><div class="sm">${esc(t)}</div></div>`).join("")}</div>
      <ul class="lines">${s.lines.map((l, k) => `<li class="build" data-step="${s.stats.length + k + 1}">${esc(l)}</li>`).join("")}</ul>
      <p class="thanks build" data-step="${s.stats.length + s.lines.length + 1}">Thank you. Questions?</p>
      <div class="foot"><span class="mono">${num} / ${TOTAL}</span></div>${notes}</section>`;
  }
  const wide = s.wide || !s.lines.length;
  return `<section class="slide content${wide ? " wide" : ""}" data-i="${i}">
    <div class="kicker">${esc(s.kicker)}</div>
    <h1>${esc(s.title)}</h1>
    <div class="body">
      ${wide ? "" : `<ul class="lines">${s.lines.map((l, k) => `<li class="build" data-step="${k + 1}">${esc(l)}</li>`).join("")}</ul>`}
      <div class="visual">${visual(s.visual)}</div>
    </div>
    <div class="foot"><span class="mono">${num} / ${TOTAL}</span></div>${notes}</section>`;
}

const html = `<title>From Knative to the Portal</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{
  --paper:#FFFFFF; --ink:#1B1F2A; --muted:#667085; --hair:#E4E7EC; --panel:#F4F5F7;
  --accent:#E4572E; --accent-ink:#FFFFFF; --teal:#0B7A75; --teal-soft:#E3F1F0; --ok:#1B8A5A; --bad:#C0392B;
  --edge:#98A2B3; --node-fill:#FFFFFF; --code-bg:#F4F5F7;
  --display:"Fraunces",Georgia,"Times New Roman",serif; --sans:"IBM Plex Sans","Helvetica Neue",Arial,sans-serif; --mono:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --paper:#111318; --ink:#F3F4F6; --muted:#9AA3B2; --hair:#262B36; --panel:#181C24;
  --accent:#FF7A52; --accent-ink:#111318; --teal:#4FB3AE; --teal-soft:#152826; --ok:#4CC38A; --bad:#F26D5B;
  --edge:#5B6472; --node-fill:#181C24; --code-bg:#181C24; } }
:root[data-theme="dark"]{
  --paper:#111318; --ink:#F3F4F6; --muted:#9AA3B2; --hair:#262B36; --panel:#181C24;
  --accent:#FF7A52; --accent-ink:#111318; --teal:#4FB3AE; --teal-soft:#152826; --ok:#4CC38A; --bad:#F26D5B;
  --edge:#5B6472; --node-fill:#181C24; --code-bg:#181C24; }
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);overflow:hidden}
.deck{position:fixed;inset:0;display:grid;place-items:center;background:var(--paper)}
.stage{position:relative;width:min(100vw,177.78vh);aspect-ratio:16/9;container-type:size;overflow:hidden;background:var(--paper)}
.slide{position:absolute;inset:0;padding:6cqh 6cqw;display:flex;flex-direction:column;opacity:0;visibility:hidden;transform:translateY(1.2cqh) scale(.985);transition:opacity .45s ease,transform .45s ease,visibility 0s .45s}
.slide.on{opacity:1;visibility:visible;transform:none;transition:opacity .45s ease,transform .45s ease,visibility 0s}
.slide.leaving{opacity:0;transform:translateY(-1.2cqh)}
.kicker{font-family:var(--mono);font-size:1.7cqw;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:500}
h1{font-family:var(--display);font-variation-settings:"opsz" 96;font-weight:800;font-size:5.6cqw;line-height:1.05;margin:1.2cqh 0 0;letter-spacing:-.01em;text-wrap:balance;max-width:80%}
.content .body{display:grid;grid-template-columns:52fr 48fr;gap:4cqw;align-items:start;margin-top:4cqh;flex:1;min-height:0;overflow:hidden}
.lines{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:1.6cqh}
.lines li{font-size:2.05cqw;line-height:1.3;font-weight:500;padding-left:2.2cqw;position:relative;text-wrap:pretty}
.lines li::before{content:"";position:absolute;left:0;top:.5em;width:.8cqw;height:.8cqw;background:var(--accent);border-radius:2px}
.visual{align-self:center;width:100%}
.content.wide .body{grid-template-columns:1fr}
.content.wide .cmp{font-size:2.2cqw}
.content.wide .cmp th{font-size:1.5cqw;padding-bottom:2cqh}
.content.wide .cmp td{padding:1.7cqh 1.6cqw}
.foot{position:absolute;left:6cqw;bottom:4cqh;display:flex;gap:2cqw;align-items:center}
.mono{font-family:var(--mono);font-size:1.4cqw;color:var(--muted)}
/* builds */
.build{opacity:0;transform:translateX(-1.6cqw);transition:opacity .45s ease,transform .6s cubic-bezier(.2,.9,.25,1.15)}
.build.on{opacity:1;transform:none}
.slide.on .build-auto{animation:rise .5s ease both;animation-delay:calc(var(--d,0ms) + .2s)}
.slide.on .graph .node.build-auto{animation:pop .55s cubic-bezier(.2,.9,.25,1.25) both;animation-delay:calc(var(--d,0ms) + .25s);transform-box:fill-box;transform-origin:center}
.slide.on .graph .edge.draw{stroke-dasharray:1;stroke-dashoffset:1;animation:draw .6s ease both;animation-delay:calc(var(--d,0ms) + .3s)}
.slide.on .title h1 .w{display:inline-block;animation:rise .6s cubic-bezier(.2,.9,.25,1.15) both;animation-delay:calc(var(--i,0) * 110ms + .15s)}
.slide.on .section h1{animation:wipe .75s cubic-bezier(.2,.8,.2,1) both}
.slide.on .section .sub{animation:rise .6s ease both;animation-delay:.5s}
@keyframes pop{from{opacity:0;transform:scale(.8)}to{opacity:1;transform:none}}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes wipe{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes rise{from{opacity:0;transform:translateY(1cqh)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.build,.slide{transition:none}.slide.on *{animation:none!important}}
/* title */
.title{justify-content:center}
.title h1{font-size:9.2cqw;max-width:75%}
.title .sub{font-size:2.6cqw;color:var(--muted);margin:3cqh 0 0;font-family:var(--display);font-weight:600;font-style:italic}
.title .meta{font-family:var(--mono);font-size:1.5cqw;color:var(--muted);margin-top:4cqh}
.pulse{position:absolute;right:8cqw;top:50%;transform:translateY(-50%);width:22cqw;height:22cqw}
.pulse span{position:absolute;inset:0;border-radius:50%;border:.25cqw solid var(--accent);opacity:0;animation:ring 3.2s ease-out infinite}
.pulse span:nth-child(2){animation-delay:1.05s}.pulse span:nth-child(3){animation-delay:2.1s}
@keyframes ring{0%{transform:scale(.2);opacity:.9}100%{transform:scale(1);opacity:0}}
@media (prefers-reduced-motion: reduce){.pulse span{animation:none;opacity:.35;transform:scale(.6)}}
/* section */
.section{background:var(--accent);color:var(--accent-ink);justify-content:center}
.section .num{font-family:var(--display);font-weight:800;font-size:22cqw;line-height:.9;letter-spacing:-.04em;opacity:.95}
.section h1{font-size:9cqw;margin-top:2cqh;max-width:90%}
.section .sub{font-family:var(--display);font-style:italic;font-weight:600;font-size:2.8cqw;margin:2cqh 0 0;opacity:.9}
/* closing */
.closing h1{font-size:6.4cqw}
.closing .stats.wide{display:grid;grid-template-columns:repeat(6,1fr);gap:2cqw;margin-top:5cqh}
.closing .lines{margin-top:5cqh;max-width:70%}
.closing .lines li{font-size:2.1cqw}
.thanks{font-family:var(--display);font-weight:800;font-size:3.4cqw;margin:auto 0 0;text-align:right}
/* visuals */
.glyph{text-align:center;padding:4cqh 0}
.glyph .g{font-family:var(--display);font-weight:800;font-size:9cqw;letter-spacing:-.02em;color:var(--teal)}
.glyph .gs{font-family:var(--mono);font-size:1.6cqw;color:var(--muted);margin-top:1cqh}
.graph{width:100%;height:auto;overflow:visible}
.graph .node rect{fill:var(--node-fill);stroke:var(--teal);stroke-width:1.6}
.graph .node.accent rect{stroke:var(--accent);stroke-width:2.2}
.graph .nl{font-family:var(--sans);font-weight:600;font-size:15px;fill:var(--ink);text-anchor:middle}
.graph .ns{font-family:var(--mono);font-size:10.5px;fill:var(--muted);text-anchor:middle}
.graph .edge{stroke:var(--edge);stroke-width:1.6;fill:none}
.stack{display:flex;flex-direction:column;gap:1.2cqh}
.layer{display:flex;justify-content:space-between;align-items:baseline;gap:2cqw;border:1.5px solid var(--teal);border-radius:.6cqw;padding:1.4cqh 1.6cqw;background:var(--node-fill)}
.layer.accent{border-color:var(--accent);border-width:2px}
.layer b{font-size:1.9cqw;font-weight:600}.layer span{font-family:var(--mono);font-size:1.25cqw;color:var(--muted);text-align:right}
.cmp{width:100%;border-collapse:collapse;font-size:1.75cqw}
.cmp th{font-family:var(--mono);font-weight:500;font-size:1.2cqw;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:0 1.2cqw 1.2cqh}
.cmp td{padding:1.4cqh 1.2cqw;border-top:1px solid var(--hair);color:var(--muted)}
.cmp td.k{font-weight:600;color:var(--ink)}.cmp td.pl{color:var(--ink)}.cmp .acc{color:var(--ink);background:var(--teal-soft)}.cmp th.acc{color:var(--teal)}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:3cqh 2cqw}
.stat .big{font-family:var(--display);font-weight:800;font-size:6.5cqw;line-height:1;color:var(--accent);letter-spacing:-.02em}
.stat .sm{font-family:var(--mono);font-size:1.35cqw;color:var(--muted);margin-top:.8cqh}
.closing .stat .big{font-size:5.5cqw}
.code{font-family:var(--mono);font-size:1.55cqw;line-height:1.75;background:var(--code-bg);border-radius:.8cqw;padding:2.5cqh 2cqw;margin:0;white-space:pre;overflow-x:auto}
.code .accent{color:var(--accent);font-weight:500}.code .ink{color:var(--ink)}.code .muted{color:var(--muted)}
.life .row{display:flex;align-items:center;gap:1cqw;flex-wrap:wrap;margin-bottom:2.5cqh}
.chip{font-weight:600;font-size:1.7cqw;padding:.9cqh 1.4cqw;border-radius:.5cqw;border:1.5px solid var(--teal);color:var(--teal)}
.chip.ok{background:var(--ok);border-color:var(--ok);color:#fff}.chip.bad{border-color:var(--bad);color:var(--bad)}
.arr{color:var(--edge);font-size:1.8cqw}.why{color:var(--muted);font-size:1.7cqw}
.reasons code{font-family:var(--mono);font-size:1.2cqw;border:1px solid var(--hair);border-radius:.4cqw;padding:.5cqh .8cqw;color:var(--bad)}
.phases{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(4,1fr);gap:1.2cqw}
.phases li{counter-increment:ph;border:1.5px solid var(--teal);border-radius:.6cqw;padding:1.4cqh 1.2cqw;background:var(--node-fill);position:relative}
.phases li::before{content:counter(ph);position:absolute;top:.8cqh;right:1cqw;font-family:var(--mono);font-size:1.1cqw;color:var(--muted)}
.phases li.ok{border-color:var(--ok)}
.phases code{display:block;font-family:var(--mono);font-weight:500;font-size:1.45cqw;padding-right:1.6cqw}
.phases span{display:block;font-size:1.25cqw;color:var(--muted);margin-top:.5cqh}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:1cqw}
.tile{border:1px solid var(--hair);border-radius:.6cqw;padding:2cqh 1.2cqw;font-size:1.45cqw;font-weight:600;color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:.5cqw;background:var(--panel)}
.tile.live{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.tile.next{border-color:var(--accent);color:var(--ink);background:var(--node-fill)}
.tile em{font-style:normal;font-family:var(--mono);font-size:1cqw;letter-spacing:.08em;text-transform:uppercase}
/* chrome */
.progress{position:absolute;left:0;top:0;height:.5cqh;background:var(--accent);width:0;transition:width .35s ease;z-index:5}
.section ~ .progress{background:var(--accent-ink)}
.help{position:fixed;right:1rem;bottom:.8rem;font-family:var(--mono);font-size:.72rem;color:var(--muted);opacity:.7}
.notespanel{position:fixed;left:0;right:0;bottom:0;max-height:34vh;overflow:auto;background:var(--panel);color:var(--ink);border-top:1px solid var(--hair);padding:1rem 1.5rem;font-size:1rem;line-height:1.5;z-index:10}
.notespanel b{font-family:var(--mono);font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);display:block;margin-bottom:.4rem}
button:focus-visible,.stage:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
</style>
<div class="deck">
  <div class="stage" id="stage" tabindex="0" aria-label="Slideshow. Use arrow keys to navigate.">
    ${slides.map(slideHtml).join("\n")}
    <div class="progress" id="progress"></div>
  </div>
</div>
<div class="help">← → navigate · N notes · Home/End</div>
<div class="notespanel" id="notes" hidden><b>Speaker notes</b><div id="notesText"></div></div>
<script>
(function(){
  var stage=document.getElementById('stage');
  var slides=Array.prototype.slice.call(stage.querySelectorAll('.slide'));
  var progress=document.getElementById('progress');
  var notesPanel=document.getElementById('notes'), notesText=document.getElementById('notesText');
  var cur=0, step=0, showNotes=false;
  var reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function maxStep(i){var m=0;slides[i].querySelectorAll('.build').forEach(function(b){m=Math.max(m,+b.dataset.step||0)});return m;}
  function paint(){
    slides.forEach(function(s,i){
      s.classList.toggle('on',i===cur);
      if(i===cur){s.querySelectorAll('.build').forEach(function(b){b.classList.toggle('on',(+b.dataset.step||0)<=step)});}
      else{s.querySelectorAll('.build').forEach(function(b){b.classList.remove('on')});}
    });
    slides.forEach(function(s,i){ if(i!==cur){ s.querySelectorAll('.big[data-n]').forEach(function(el){ el.dataset.done=''; el.textContent=el.dataset.n; }); } });
    slides[cur].querySelectorAll('.big[data-n]').forEach(function(el){
      var host=el.closest('.build'); if(host && !host.classList.contains('on')) return;
      if(el.dataset.done) return; el.dataset.done='1';
      var n=parseInt(el.dataset.n,10); if(isNaN(n)||reduced){ el.textContent=el.dataset.n; return; }
      var t0=null; function tick(ts){ if(t0===null) t0=ts; var p=Math.min(1,(ts-t0)/900); var e=1-Math.pow(1-p,3); el.textContent=Math.round(n*e); if(p<1) requestAnimationFrame(tick); else el.textContent=el.dataset.n; }
      requestAnimationFrame(tick);
    });
    progress.style.width=((cur+1)/slides.length*100)+'%';
    var n=slides[cur].querySelector('.notes');
    notesText.textContent=n?n.textContent:'(no notes)';
    notesPanel.hidden=!showNotes;
    try{history.replaceState(null,'','#'+(cur+1)+(step?'.'+step:''));}catch(e){}
  }
  function next(){ if(step<maxStep(cur)){step++;} else if(cur<slides.length-1){cur++;step=0;} paint(); }
  function prev(){ if(step>0){step--;} else if(cur>0){cur--;step=maxStep(cur);} paint(); }
  function go(i,s){cur=Math.max(0,Math.min(slides.length-1,i));step=Math.min(s||0,maxStep(cur));paint();}
  document.addEventListener('keydown',function(e){
    if(e.metaKey||e.ctrlKey||e.altKey)return;
    switch(e.key){
      case 'ArrowRight':case 'ArrowDown':case ' ':case 'PageDown':case 'Enter':e.preventDefault();next();break;
      case 'ArrowLeft':case 'ArrowUp':case 'PageUp':case 'Backspace':e.preventDefault();prev();break;
      case 'Home':e.preventDefault();go(0,0);break;
      case 'End':e.preventDefault();go(slides.length-1,999);break;
      case 'n':case 'N':showNotes=!showNotes;paint();break;
      case 'f':case 'F':if(document.fullscreenElement){document.exitFullscreen();}else if(stage.requestFullscreen){stage.requestFullscreen();}break;
    }
  });
  stage.addEventListener('click',function(e){ if(e.clientX<window.innerWidth*0.2){prev();}else{next();} });
  var tx=null;stage.addEventListener('touchstart',function(e){tx=e.touches[0].clientX},{passive:true});
  stage.addEventListener('touchend',function(e){ if(tx===null)return; var dx=e.changedTouches[0].clientX-tx; if(dx<-40)next(); else if(dx>40)prev(); tx=null; });
  function fromHash(){ var m=/^#(\\d+)(?:\\.(\\d+))?/.exec(location.hash||''); if(m){go(+m[1]-1,+(m[2]||0));} else {paint();} }
  window.addEventListener('hashchange',fromHash); fromHash();
  stage.focus();
})();
</script>
`;
fs.writeFileSync(__dirname + "/deck.html", html);
console.log("wrote deck.html", (html.length / 1024).toFixed(0) + "KB", TOTAL, "slides");
