// Edge anchoring shared by both renderers. Nodes are {x,y,w,h} in the 600x440 box.
function anchors(a, b) {
  const ac = { x: a.x + a.w / 2, y: a.y + a.h / 2 };
  const bc = { x: b.x + b.w / 2, y: b.y + b.h / 2 };
  const dx = bc.x - ac.x, dy = bc.y - ac.y;
  // Prefer the axis with the larger clear gap between boxes.
  const gapX = Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w));
  const gapY = Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h));
  if (gapX >= gapY) {
    if (dx >= 0) return { x1: a.x + a.w, y1: ac.y, x2: b.x, y2: bc.y };
    return { x1: a.x, y1: ac.y, x2: b.x + b.w, y2: bc.y };
  }
  if (dy >= 0) return { x1: ac.x, y1: a.y + a.h, x2: bc.x, y2: b.y };
  return { x1: ac.x, y1: a.y, x2: bc.x, y2: b.y + b.h };
}
module.exports = { anchors };
