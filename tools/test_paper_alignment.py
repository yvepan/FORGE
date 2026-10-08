import argparse
import ast
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import run


class ProfileTests(unittest.TestCase):
    def test_qp_uses_frozen_gemini_helper_with_gpt_victim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'runs/test'
            target.mkdir(parents=True)
            (root / 'models/bge-small-en-v1.5').mkdir(parents=True)
            (root / 'models/bge-small-en-v1.5/config.json').write_text('{}')
            (target / 'protocol.json').write_text(json.dumps({
                'framework': 'gpt-researcher', 'victim_model': 'gpt-victim',
                'auxiliary_model': 'gpt-victim', 'defense_mode': 'query_paraphrasing',
                'defense_parameters': {'rewrite_model': 'gemini-3.6-flash'}}))
            with patch.object(run, 'ROOT', root), patch.dict(os.environ,
                    FORGE_DEFENSE_REWRITE_MODEL='incorrect-override'):
                env = run.environment(target, 'gpt-researcher')
            self.assertEqual(env['DR_MODEL'], 'gpt-victim')
            self.assertEqual(env['DR_AUX_MODEL'], 'gpt-victim')
            self.assertEqual(env['FORGE_DEFENSE_REWRITE_MODEL'], 'gemini-3.6-flash')

    def test_model_and_webthinker_caps_frozen_at_init(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'runs/input/articles.sqlite'
            source.parent.mkdir(parents=True)
            with closing(sqlite3.connect(source)) as db:
                db.executescript('CREATE TABLE articles(id INTEGER PRIMARY KEY,url,title,text); CREATE VIRTUAL TABLE article_fts USING fts5(title,text,content=articles,content_rowid=id); CREATE INDEX articles_title_lookup ON articles(title); INSERT INTO articles VALUES(1,"u","t","x"); INSERT INTO article_fts(rowid,title,text) VALUES(1,"t","x");')
            shared = root / 'code/shared/src'
            shared.mkdir(parents=True)
            (shared / 'wiki_sandbox.py').write_text('# fixture')
            config = root / 'code/frameworks/webthinker/config'
            config.mkdir(parents=True)
            for filename in ('adapter.json', 'run.json'):
                (config / filename).write_text('{}')
            dataset = source.parent / 'dataset.json'
            dataset.write_text(json.dumps({'database': str(source), 'name': 'clean', 'documents': [], 'queries': [{'query_id': 'q', 'query': 'question', 'target_claim': 'target', 'adoption_rule': 'rule'}]}))
            args = argparse.Namespace(name='test', dataset=str(dataset), query_id='q', framework='webthinker', family='gemini', model='explicit-api-id', defense='none')
            with patch.object(run, 'ROOT', root), patch.object(run, 'N', 1):
                run.prepare(args)
            resolved = json.loads((root / 'runs/test/webthinker/config/adapter.json').read_text())
            self.assertEqual(resolved['primary_model'], resolved['auxiliary_model'])
            self.assertEqual(resolved['max_output_tokens_by_model']['explicit-api-id'], 16384)
            self.assertEqual(resolved['final_report_max_output_tokens'], 16384)
            (root / 'models/bge-small-en-v1.5').mkdir(parents=True)
            (root / 'models/bge-small-en-v1.5/config.json').write_text('{}')
            with patch.object(run, 'ROOT', root), patch.dict(os.environ, {'DR_MODEL': 'incorrect-override', 'FORGE_DEFENSE_MODE': 'query_paraphrasing'}):
                environment = run.environment(root / 'runs/test', 'webthinker')
            self.assertEqual(environment['DR_MODEL'], 'explicit-api-id')
            self.assertEqual(environment['DR_AUX_MODEL'], 'explicit-api-id')
            self.assertEqual(environment['FORGE_DEFENSE_MODE'], 'none')
            with patch.object(run, 'ROOT', root):
                run.verify_freeze(root / 'runs/test')
                protocol = root / 'runs/test/protocol.json'
                original = protocol.read_text()
                protocol.write_text(original + ' ')
                with self.assertRaisesRegex(ValueError, 'Frozen protocol/source'):
                    run.verify_freeze(root / 'runs/test')
                protocol.write_text(original)
                with source.open('ab') as stream:
                    stream.write(b'changed corpus')
                with self.assertRaisesRegex(ValueError, 'Frozen corpus'):
                    run.verify_freeze(root / 'runs/test')

    def test_rqa_completion_contains_actual_context_without_extra_system(self):
        source = Path(__file__).resolve().parents[1] / 'code/frameworks/webthinker/src/run_full_dr.py'
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'anchor_completion_chatml')
        policy = Path(__file__).resolve().parents[1] / 'code/shared/src/defense_policies.py'
        policy_functions = [n for n in ast.parse(policy.read_text()).body if isinstance(n, ast.FunctionDef) and n.name in ('anchor_recursive_prompt', 'expanded_candidate_k')]
        scope = {'QUESTION': 'Original root'}
        exec(compile(ast.Module(body=policy_functions + [function], type_ignores=[]), '<real functions>', 'exec'), scope)
        prompt = '<|im_start|>system\nNative system<|im_end|>\n<|im_start|>user\nActual context and learnings<|im_end|>\n<|im_start|>assistant\nExisting prefix'
        anchored, changed = scope['anchor_completion_chatml'](prompt)
        self.assertTrue(changed)
        self.assertIn('CURRENT SUBTASK OR LEARNINGS:\nActual context and learnings', anchored)
        self.assertEqual(anchored.count('<|im_start|>system'), 1)
        self.assertTrue(anchored.endswith('<|im_start|>assistant\nExisting prefix'))
        for k in (1, 5, 25, 40, 100):
            with patch.dict(os.environ, FORGE_K_MAX='999', FORGE_K_EXPANSION_FACTOR='10'):
                self.assertEqual(scope['expanded_candidate_k'](k), min(50, 2*k))

    def test_webthinker_completion_uses_defined_state_and_fixed_budget(self):
        source = Path(__file__).resolve().parents[1] / 'code/frameworks/webthinker/src/run_full_dr.py'
        tree = ast.parse(source.read_text())
        main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main')
        self.assertNotIn('strict_completion', {n.id for n in ast.walk(main) if isinstance(n, ast.Name)})
        cap = next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'edit_cap' for t in n.targets))
        self.assertEqual(ast.literal_eval(cap.value), 8)

    def test_gpt_report_truncation_not_accepted(self):
        source = Path(__file__).resolve().parents[1] / 'code/frameworks/gpt-researcher/src/run.py'
        function = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_complete_report_stream')
        scope = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<report completion guard>', 'exec'), scope)
        async def run_case(reason):
            async def stream(*args, **kwargs):
                yield SimpleNamespace(content='Complete paragraph\n', response_metadata={})
                yield SimpleNamespace(content='', response_metadata={'finish_reason': reason})
            async def output(*args):
                pass
            provider = SimpleNamespace(llm=SimpleNamespace(astream=stream), _send_output=output)
            return await scope['_complete_report_stream'](provider, [])
        def complete(coroutine):
            try:
                coroutine.send(None)
            except StopIteration as result:
                return result.value
            raise AssertionError('Fixture unexpectedly requires an event loop')
        self.assertEqual(complete(run_case('stop')), 'Complete paragraph\n')
        for reason in ('length', None, 'content_filter'):
            with self.assertRaisesRegex(RuntimeError, 'normal completion'):
                complete(run_case(reason))


if __name__ == '__main__':
    unittest.main()
