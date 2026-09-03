const sharp = require("sharp");
const ReactDOMServer = require("react-dom/server");
const React = require("react");

async function gradientBg({ w = 1920, h = 1080, base = "#0B1120", glows = [] } = {}) {
  const defs = glows.map((g, i) =>
    `<radialGradient id="g${i}" cx="${g.cx}" cy="${g.cy}" r="${g.r}" gradientUnits="userSpaceOnUse">
       <stop offset="0" stop-color="${g.color}" stop-opacity="${g.op ?? 0.5}"/>
       <stop offset="1" stop-color="${g.color}" stop-opacity="0"/>
     </radialGradient>`).join("");
  const rects = glows.map((g, i) => `<rect width="${w}" height="${h}" fill="url(#g${i})"/>`).join("");
  // subtle dot grid for texture
  const dots = `<pattern id="dots" width="48" height="48" patternUnits="userSpaceOnUse"><circle cx="1.5" cy="1.5" r="1.2" fill="#FFFFFF" fill-opacity="0.045"/></pattern>`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><defs>${defs}${dots}</defs>
    <rect width="${w}" height="${h}" fill="${base}"/>${rects}<rect width="${w}" height="${h}" fill="url(#dots)"/></svg>`;
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

async function icon(IconComp, { color = "#FFFFFF", size = 256 } = {}) {
  let svg = ReactDOMServer.renderToStaticMarkup(React.createElement(IconComp, { color, size }));
  svg = svg.replace(/currentColor/g, color);
  const buf = await sharp(Buffer.from(svg)).resize(size, size).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

module.exports = { gradientBg, icon };
