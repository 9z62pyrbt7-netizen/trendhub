import { themeCheckRun } from '@shopify/theme-check-node';
import path from 'node:path';
const root = process.argv[2] || path.resolve(path.dirname(new URL(import.meta.url).pathname), '../theme');
const res = await themeCheckRun(root, undefined, (m)=>{});
const offenses = res.offenses || res;
for (const o of offenses) {
  console.log(`${['ERR','WARN','INFO'][o.severity]||o.severity} ${o.uri.replace(/.*storefront\//,'')}:${o.start?.line+1} [${o.check}] ${o.message}`);
}
console.log('total', offenses.length);
if (offenses.some((o) => o.severity === 0)) process.exit(1);
