"""Offline construction-contract checks with a deterministic model double."""
import importlib.util
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'code/construction'
with patch.object(sys, 'path', [str(SOURCE), *sys.path]):
    spec = importlib.util.spec_from_file_location('forge_construction', SOURCE / 'construct.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

VALUE = {'target_narrative': 'Target', 'topic_keywords': ['Topic'], 'question_direction': 'Compare alternatives'}


class Model:
    def __init__(self, failures=0):
        self.failures = failures
        self.audits = 0
        self.calls = []

    def __call__(self, prompt, value, generation=False):
        self.calls.append((prompt, deepcopy(value), generation))
        if prompt == module.CHAIN:
            return {'steps': [{'claim': str(i) if i < 5 else 'Target', 'dependency': 'prior premise'} for i in range(1, 6)]}
        if prompt in (module.DRAFT, module.REPAIR):
            return {'title': 'Argument', 'body': ' '.join([f"word{value['number']}"] * 200)}
        if prompt == module.EXTRACT:
            return {'conclusion_quotes': [value['body']], 'qualification_quotes': [], 'summary': value['body'], 'dependencies': []}
        if prompt == module.OBJECTIONS:
            return {'objections': [{'objection': str(i), 'counterevidence': 'counter', 'response': 'answer', 'qualifications': 'limits'} for i in range(5)]}
        if prompt == module.DOCUMENT_REVIEW:
            d = value['document']
            if d['number'] == 1:
                self.audits += 1
            fail = d['number'] == 2 and self.audits <= self.failures
            return {'passed': not fail, 'issues': [{'quote': d['body'], 'reason': 'broken dependency'}] if fail else []}
        if prompt == module.PAIR_REVIEW:
            return {'passed': True, 'issues': []}
        raise AssertionError(prompt)


class ConstructionTests(unittest.TestCase):
    def build(self, model):
        with tempfile.TemporaryDirectory() as tmp:
            return module.Builder(model, VALUE).build(Path(tmp))

    def test_input_excludes_full_question(self):
        with self.assertRaises(ValueError):
            module.inputs({**VALUE, 'question': 'full original root question'})

    def test_sequential_core_completed_before_objections_and_all_pairs(self):
        model = Model()
        docs, repairs = self.build(model)
        drafts = [v for p, v, g in model.calls if p == module.DRAFT]
        self.assertEqual([len(v['actual_core_summaries']) for v in drafts[:5]], [0, 1, 2, 3, 4])
        self.assertTrue(all(len(v['actual_core_summaries']) == 5 for v in drafts[5:]))
        self.assertEqual(sum(p == module.PAIR_REVIEW for p, v, g in model.calls), 45)
        self.assertEqual(len(docs), 10)
        self.assertEqual(repairs, 0)
        self.assertTrue(all(d['prefix'] == 'Topic: Topic.' for d in docs))

    def test_repair_refreshes_downstream_and_all_objections(self):
        model = Model(failures=1)
        _, repairs = self.build(model)
        repaired = [v['number'] for p, v, g in model.calls if p == module.REPAIR]
        self.assertEqual(repaired, list(range(2, 11)))
        self.assertEqual(repairs, 1)
        self.assertEqual(sum(p == module.OBJECTIONS for p, v, g in model.calls), 2)

    def test_exact_seven_round_limit(self):
        model = Model(failures=8)
        with self.assertRaisesRegex(ValueError, 'seven repair'):
            self.build(model)
        self.assertEqual(model.audits, 8)
        self.assertEqual(sum(p == module.PAIR_REVIEW for p, v, g in model.calls), 45 * 8)

    def test_invalid_review_evidence_rejected(self):
        builder = module.Builder(Model(), VALUE)
        with self.assertRaises(ValueError):
            builder.checked_review({'passed': False, 'issues': [{'quote': 'invented', 'reason': 'x'}]}, [{'number': 1, 'body': 'actual'}])


if __name__ == '__main__':
    unittest.main()
