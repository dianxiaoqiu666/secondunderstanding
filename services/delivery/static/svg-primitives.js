// Shared SVG serialization only; no CAD state or geometry inference.
const NS = 'http://www.w3.org/2000/svg';

export const pointsAttr = points => points.map(p=>`${p[0]},${-p[1]}`).join(' ');

export function node(tag,attrs={},text) {
  const n = document.createElementNS(NS,tag);
  Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));
  if (text !== undefined) n.textContent = text;
  return n;
}

export function pathForPolygon(data) {
  const rings = [data.boundary_mm || [], ...(data.holes_mm || [])];
  return rings.filter(p=>p.length>=3).map(p=>'M'+p.map(([x,y])=>`${x},${-y}`).join('L')+'Z').join(' ');
}
