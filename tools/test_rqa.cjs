const assert = require('node:assert/strict');
const path = require('node:path');
const vm = require('node:vm');
const {build} = require('esbuild');
const root = path.resolve(__dirname, '..');

(async () => {
  const result = await build({
    stdin: {contents: `export {anchorPlanningMessages} from './code/frameworks/perplexica/src/rqa';
      export {getResearcherPrompt} from './vendor/perplexica/src/lib/prompts/search/researcher';
      export {getWriterPrompt} from './vendor/perplexica/src/lib/prompts/search/writer';`,
      resolveDir: root, sourcefile: 'rqa-test.ts'},
    tsconfig: path.join(root, 'vendor/perplexica/tsconfig.json'),
    bundle: true, write: false, platform: 'node', format: 'cjs',
    plugins: [{name: 'no-upload-fixtures', setup(builder) {
      builder.onResolve({filter: /^@\/lib\/uploads\/store$/}, () => ({path: 'uploads', namespace: 'fixture'}));
      builder.onLoad({filter: /.*/, namespace: 'fixture'}, () => ({contents: 'export default {getFileData: () => []};', loader: 'ts'}));
    }}],
  });
  const module = {exports: {}};
  vm.runInNewContext(result.outputFiles[0].text, {module, exports: module.exports, require, console});
  const {anchorPlanningMessages, getResearcherPrompt, getWriterPrompt} = module.exports;
  const question = 'Compare A and B. Use only the supplied local Wikipedia snapshot.';
  for (const mode of ['speed', 'balanced', 'quality']) {
    const planner = getResearcherPrompt('search', mode, 0, 25, []);
    const messages = [{role: 'system', content: planner}, {role: 'user', content: question}];
    const anchored = anchorPlanningMessages(messages, question);
    assert(anchored[0].content.startsWith('ROOT USER QUERY'));
    assert(anchored[0].content.includes(question));
    assert(anchored[0].content.includes(planner));
    assert.equal(anchored[1], messages[1]);
    // A writer containing quoted planner text is still a writer, not a planning call.
    const writer = {role: 'system', content: getWriterPrompt(planner, '', mode)};
    assert.equal(anchorPlanningMessages([writer], question)[0], writer);
  }
  console.log('RQA: all native planning modes anchored; all final writer modes unchanged.');
})().catch(error => {console.error(error); process.exitCode = 1;});
