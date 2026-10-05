const path = require('node:path');
const { buildSync } = require('esbuild');
const root = path.resolve(__dirname, '..');
buildSync({
  entryPoints: [path.join(root, 'code/frameworks/perplexica/src/run.ts')],
  outfile: path.join(root, 'code/frameworks/perplexica/run.cjs'),
  bundle: true, platform: 'node', format: 'cjs', target: 'node22', packages: 'external',
  tsconfig: path.join(root, 'vendor/perplexica/tsconfig.json'),
});
console.log('Built portable launcher from unchanged native Perplexica modules.');
