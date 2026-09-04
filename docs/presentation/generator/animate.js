// Post-process a pptxgenjs deck: add a fade slide transition and click-to-reveal fade
// entrances for every shape whose name is "step:N" (grouped by N, ascending).
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const [,, inFile, outFile] = process.argv;
const work = path.join(__dirname, "unpacked");
fs.rmSync(work, { recursive: true, force: true });
fs.mkdirSync(work);
execSync(`cd "${work}" && unzip -q "${inFile}"`);

const slidesDir = path.join(work, "ppt/slides");
let animated = 0;
for (const f of fs.readdirSync(slidesDir).filter((f) => /^slide\d+\.xml$/.test(f))) {
  const p = path.join(slidesDir, f);
  let xml = fs.readFileSync(p, "utf8");
  // collect (id, kind, step, effect, isText) in document order
  const items = [];
  const re = /<p:(sp|pic|cxnSp)>[\s\S]*?<p:cNvPr id="(\d+)" name="([^"]*)"/g;
  let m;
  while ((m = re.exec(xml))) {
    const sm = /^(step|auto):(\d+)(?::(\w+))?$/.exec(m[3]);
    if (sm) items.push({ id: m[2], kind: sm[1], step: +sm[2], eff: sm[3] || "fade", text: m[1] === "sp" });
  }
  let id = 1;
  const nid = () => id++;
  const rootId = nid(), seqId = nid();
  const PRESET = { fade: [10, 0], wipeL: [22, 8], wipeD: [22, 4], zoom: [53, 0] };
  const tgt = (spid) => `<p:tgtEl><p:spTgt spid="${spid}"/></p:tgtEl>`;
  function effectXml(spid, eff, nodeType, delay) {
    const [pid, sub] = PRESET[eff] || PRESET.fade;
    let inner = `<p:set><p:cBhvr><p:cTn id="${nid()}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>${tgt(spid)}<p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>`;
    if (eff === "wipeL") inner += `<p:animEffect transition="in" filter="wipe(left)"><p:cBhvr><p:cTn id="${nid()}" dur="350"/>${tgt(spid)}</p:cBhvr></p:animEffect>`;
    else if (eff === "wipeD") inner += `<p:animEffect transition="in" filter="wipe(down)"><p:cBhvr><p:cTn id="${nid()}" dur="350"/>${tgt(spid)}</p:cBhvr></p:animEffect>`;
    else if (eff === "zoom") inner += `<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="${nid()}" dur="450"/>${tgt(spid)}</p:cBhvr></p:animEffect><p:animScale><p:cBhvr><p:cTn id="${nid()}" dur="450" fill="hold"/>${tgt(spid)}</p:cBhvr><p:from x="60000" y="60000"/><p:to x="100000" y="100000"/></p:animScale>`;
    else inner += `<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="${nid()}" dur="400"/>${tgt(spid)}</p:cBhvr></p:animEffect>`;
    return `<p:par><p:cTn id="${nid()}" presetID="${pid}" presetClass="entr" presetSubtype="${sub}" fill="hold" grpId="0" nodeType="${nodeType}"><p:stCondLst><p:cond delay="${delay}"/></p:stCondLst><p:childTnLst>${inner}</p:childTnLst></p:cTn></p:par>`;
  }
  const autos = items.filter((i) => i.kind === "auto").sort((a, b) => a.step - b.step);
  const steps = [...new Set(items.filter((i) => i.kind === "step").map((i) => i.step))].sort((a, b) => a - b);
  let pars = "";
  if (autos.length) {
    const effects = autos.map((a, k) => effectXml(a.id, a.eff, k === 0 ? "afterEffect" : "withEffect", k * 80)).join("");
    pars += `<p:par><p:cTn id="${nid()}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/><p:cond evt="onBegin" delay="0"><p:tn val="${seqId}"/></p:cond></p:stCondLst><p:childTnLst><p:par><p:cTn id="${nid()}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>${effects}</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>`;
  }
  pars += steps.map((st) => {
    const members = items.filter((i) => i.kind === "step" && i.step === st);
    const effects = members.map((mb, k) => effectXml(mb.id, mb.eff, k === 0 ? "clickEffect" : "withEffect", 0)).join("");
    return `<p:par><p:cTn id="${nid()}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst><p:par><p:cTn id="${nid()}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>${effects}</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>`;
  }).join("");
  const timing = items.length ? `<p:timing><p:tnLst><p:par><p:cTn id="${rootId}" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst><p:seq concurrent="1" nextAc="seek"><p:cTn id="${seqId}" dur="indefinite" nodeType="mainSeq"><p:childTnLst>${pars}</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst><p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq></p:childTnLst></p:cTn></p:par></p:tnLst><p:bldLst>${items.filter((i) => i.text).map((i) => `<p:bldP spid="${i.id}" grpId="0"/>`).join("")}</p:bldLst></p:timing>` : "";
  const transition = `<p:transition spd="med"><p:fade/></p:transition>`;
  // insert after </p:clrMapOvr> (CT_Slide order: cSld, clrMapOvr, transition, timing, extLst)
  if (!xml.includes("</p:clrMapOvr>")) throw new Error("no clrMapOvr in " + f);
  xml = xml.replace("</p:clrMapOvr>", "</p:clrMapOvr>" + transition + timing);
  fs.writeFileSync(p, xml);
  if (items.length) animated++;
}
fs.rmSync(outFile, { force: true });
execSync(`cd "${work}" && zip -qXr "${outFile}" .`);
console.log("animated slides:", animated, "->", outFile);
