// Test için beyaz arka planlı çanta görselleri üretir (gerçek ürün görsellerinin yerine; CDN erişimi olmadan).
import fs from 'node:fs';
import sharp from 'sharp';
const out = new URL('./.cache/img/', import.meta.url).pathname;
fs.mkdirSync(out, { recursive: true });
const bag = (body, trim, variant, w, h) => {
  // variant: 0 front, 1 angled, 2 close, 3 side, 4 open ...
  const cx = w/2, cy = h/2 + 60;
  const s = [1, .92, 1.6, .8, 1, .95, 1.2, .85, 1][variant] || 1;
  const rot = [0, -8, 0, 10, 0, 5, -4, 0, 12][variant] || 0;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <g transform="translate(${cx} ${cy}) rotate(${rot}) scale(${s})">
    <ellipse cx="0" cy="330" rx="420" ry="34" fill="#000" opacity=".08"/>
    <path d="M-250 -200 C -250 -420, 250 -420, 250 -200" fill="none" stroke="${trim}" stroke-width="34" stroke-linecap="round"/>
    <path d="M-440 -180 L440 -180 L500 300 Q 500 330 470 330 L-470 330 Q-500 330 -500 300 Z" fill="${body}"/>
    <path d="M-440 -180 L440 -180 L455 -60 L-455 -60 Z" fill="#000" opacity=".18"/>
    <rect x="-70" y="-90" width="140" height="70" rx="12" fill="#c9a45c"/>
    <path d="M-470 300 L470 300" stroke="#fff" stroke-opacity=".12" stroke-width="6" stroke-dasharray="14 12"/>
    <circle cx="-380" cy="-150" r="18" fill="#c9a45c"/><circle cx="380" cy="-150" r="18" fill="#c9a45c"/>
  </g></svg>`;
};
const sizes = [[1500,1422],[1500,1500],[1500,1500],[1451,1500],[1386,1500],[1500,1448],[1466,1424],[1393,1380],[1500,1247]];
const sets = { black: ['#1b1917','#2a2622'], brown: ['#6b4430','#4a2e20'], beige: ['#d9c4a5','#b89a74'] };
for (const [name, [body, trim]] of Object.entries(sets)) {
  for (let i = 0; i < 9; i++) {
    const [w, h] = sizes[i];
    await sharp(Buffer.from(bag(body, trim, i, w, h))).jpeg({ quality: 82 }).toFile(`${out}${name}-${i}.jpg`);
  }
}
console.log('ok');
