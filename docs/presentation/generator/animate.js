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
  // collect (id, step, isText) in document order
  const items = [];
  const re = /<p:(sp|pic|cxnSp)>[\s\S]*?<p:cNvPr id="(\d+)" name="([^"]*)"/g;
  let m;
  while ((m = re.exec(xml))) {
    const sm = /^step:(\d+)$/.exec(m[3]);
    if (sm) items.push({ id: m[2], step: +sm[1], text: m[1] === "sp" });
  }
  const steps = [...new Set(items.map((i) => i.step))].sort((a, b) => a - b);
  let id = 1;
  const nid = () => id++;
  const rootId = nid(), seqId = nid();
  const pars = steps.map((st) => {
    const members = items.filter((i) => i.step === st);
    const effects = members.map((mb, k) => {
      const ctn = nid(), setId = nid(), fxId = nid();
      return `<p:par><p:cTn id="${ctn}" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" nodeType="${k === 0 ? "clickEffect" : "withEffect"}"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>` +
        `<p:set><p:cBhvr><p:cTn id="${setId}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="${mb.id}"/></p:tgtEl><p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>` +
        `<p:animEffect transition="in" filter="fade"><p:cBhvr><p:cTn id="${fxId}" dur="400"/><p:tgtEl><p:spTgt spid="${mb.id}"/></p:tgtEl></p:cBhvr></p:animEffect>` +
        `</p:childTnLst></p:cTn></p:par>`;
    }).join("");
    const clickId = nid(), innerId = nid();
    return `<p:par><p:cTn id="${clickId}" fill="hold"><p:stCondLst><p:cond delay="indefinite"/></p:stCondLst><p:childTnLst><p:par><p:cTn id="${innerId}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>${effects}</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>`;
  }).join("");
  const timing = steps.length ? `<p:timing><p:tnLst><p:par><p:cTn id="${rootId}" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst><p:seq concurrent="1" nextAc="seek"><p:cTn id="${seqId}" dur="indefinite" nodeType="mainSeq"><p:childTnLst>${pars}</p:childTnLst></p:cTn><p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst><p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst></p:seq></p:childTnLst></p:cTn></p:par></p:tnLst><p:bldLst>${items.filter((i) => i.text).map((i) => `<p:bldP spid="${i.id}" grpId="0"/>`).join("")}</p:bldLst></p:timing>` : "";
  const transition = `<p:transition spd="med"><p:fade/></p:transition>`;
  // insert after </p:clrMapOvr> (CT_Slide order: cSld, clrMapOvr, transition, timing, extLst)
  if (!xml.includes("</p:clrMapOvr>")) throw new Error("no clrMapOvr in " + f);
  xml = xml.replace("</p:clrMapOvr>", "</p:clrMapOvr>" + transition + timing);
  fs.writeFileSync(p, xml);
  if (steps.length) animated++;
}
fs.rmSync(outFile, { force: true });
execSync(`cd "${work}" && zip -qXr "${outFile}" .`);
console.log("animated slides:", animated, "->", outFile);
