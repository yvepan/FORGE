import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AdditionTests(unittest.TestCase):
    def test_judge_rule_order_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, FORGE_SHARED_DIR=directory):
                module = load('judge_test', ROOT / 'code/shared/src/llm_document_judge.py')
            value = dict(relevance=1, evidence_quality=1, credibility=1,
                         manipulation_risk=5, instruction_injection=False, decision='REJECT')
            self.assertTrue(module.validate(value))
            value['instruction_injection'] = True
            value['decision'] = 'KEEP'
            self.assertFalse(module.validate(value))
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
