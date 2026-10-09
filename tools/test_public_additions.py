import importlib.util
from contextlib import closing
import json
import math
import os
from pathlib import Path
import tempfile
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdditionTests(unittest.TestCase):
    def test_ppl_weights_shifted_targets_without_truncating_tail(self):
        import torch
        module = load('ppl_tokens_test', ROOT / 'code/shared/src/ppl_filter.py')
        calls = []
        class Model:
            config = SimpleNamespace(n_positions=4)
            def __call__(self, chunk, labels):
                calls.append(labels[:, 1:][labels[:, 1:] != -100].tolist())
                return SimpleNamespace(loss=torch.tensor(float(len(calls))))
        scorer = module.GPT2Perplexity.__new__(module.GPT2Perplexity)
        scorer.torch = torch
        scorer.model = Model()
        scorer.tokenizer = lambda *args, **kwargs: {'input_ids': torch.arange(7).reshape(1, 7)}
        self.assertAlmostEqual(scorer('full article'), math.exp((3 + 4 + 3) / 6))
        self.assertEqual(calls, [[1, 2, 3], [4, 5], [6]])

    def test_ppl_filters_clean_and_attack_before_retrieval(self):
        module = load('ppl_test', ROOT / 'code/shared/src/ppl_filter.py')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.sqlite'
            with closing(sqlite3.connect(source)) as db, db:
                db.executescript("CREATE TABLE articles(id INTEGER PRIMARY KEY,url,title,text);"
                    "CREATE VIRTUAL TABLE article_fts USING fts5(title,text,content=articles,content_rowid=id,tokenize='porter');"
                    "CREATE INDEX articles_title_lookup ON articles(title);")
                db.executemany('INSERT INTO articles VALUES(?,?,?,?)',
                    [(1, 'u1', 'clean kept', 'boundary'), (2, 'u2', 'clean removed', 'high'),
                     (3, 'u3', 'attack removed', 'high'), (4, 'u4', 'attack kept', 'low')])
                db.execute("INSERT INTO article_fts(article_fts) VALUES('rebuild')")
            documents = [dict(id=i, url='u'+str(i), title=title, text=text)
                         for i, title, text in [(3, 'attack removed', 'high'), (4, 'attack kept', 'low')]]
            manifest = root / 'input.json'
            manifest.write_text(json.dumps(dict(database=str(source), name='FORGE',
                base_article_count=2, documents=documents, queries=[])))
            result = module.filter_corpus(manifest, root / 'filtered.sqlite', root / 'filtered.json',
                lambda text: {'boundary': 111.71, 'high': 111.72, 'low': 10}[text])
            self.assertEqual(result['base_article_count'], 1)
            self.assertEqual([d['id'] for d in result['documents']], [4])
            self.assertEqual(result['ppl_filter']['removed_attack_ids'], [3])
            with closing(sqlite3.connect(root / 'filtered.sqlite')) as db:
                self.assertEqual(db.execute('SELECT id FROM articles ORDER BY id').fetchall(), [(1,), (4,)])
                self.assertEqual(db.execute("SELECT rowid FROM article_fts WHERE article_fts MATCH 'removed'").fetchall(), [])
                self.assertEqual(db.execute('SELECT count(*) FROM article_fts_docsize').fetchone()[0], 2)
            with closing(sqlite3.connect(source)) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM articles').fetchone()[0], 4)

    def test_judge_rule_order_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, FORGE_SHARED_DIR=directory):
                module = load('judge_test', ROOT / 'code/shared/src/llm_document_judge.py')
            value = dict(relevance=1, evidence_quality=1, credibility=1,
                         manipulation_risk=5, instruction_injection=False, decision='REJECT')
            for injection, risk, keep in ((False, 3, True), (False, 4, False),
                                          (True, 3, False), (True, 4, False)):
                value.update(instruction_injection=injection, manipulation_risk=risk,
                             decision='REJECT' if keep else 'KEEP')
                self.assertEqual(module.validate(value), keep)
            value['instruction_injection'] = 'true'
            with self.assertRaises(ValueError):
                module.validate(value)
            documents = [dict(id=i, title=str(i), url=str(i), text='body', snippet='snippet') for i in range(4)]
            def verdict(document, *args):
                if document['id'] == 2:
                    raise RuntimeError('unavailable')
                return document['id'] != 1
            with patch.object(module, 'judge', side_effect=verdict):
                kept, audit = module.filter_documents(documents, 'query', 'test')
            self.assertEqual([d['id'] for d in kept], [0, 3])
            self.assertEqual(audit['rejected'], 1)
            self.assertEqual(audit['errors'], 1)

    def test_baseline_budget_and_restricted_inputs(self):
        import sys
        with patch.object(sys, 'path', [str(ROOT / 'code/baselines'), *sys.path]):
            module = load('baseline_test', ROOT / 'code/baselines/poisonedrag.py')
        value = dict(target_narrative='Synthetic target', topic_keywords=['topic'], question_direction='comparison')
        calls = []
        def generate(request):
            calls.append(request)
            return {'title': 'Synthetic passage', 'body': 'word ' * 200}
        documents = module.construct(value, generate, lambda d: True)
        self.assertEqual(len(documents), 10)
        self.assertEqual(len(calls), 10)
        with self.assertRaises(ValueError):
            module.construct({**value, 'full_question': 'excluded'}, generate, lambda d: True)
        calls.clear()
        with self.assertRaises(ValueError):
            module.construct(value, generate, lambda d: False)
        self.assertEqual(len(calls), 8)
