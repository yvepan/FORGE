import copy
import csv
import importlib.util
from pathlib import Path
import unittest
import tempfile

spec = importlib.util.spec_from_file_location('paper_metrics', Path(__file__).resolve().parents[1] / 'code/evaluation/evaluate.py')
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def row(question, positives, total):
    units = [{'id': str(i), 'label': int(i < positives), 'quote': 'span',
              'evidence_pointer': 'external trace'} for i in range(total)]
    return {'model': 'test', 'framework': 'test', 'condition': 'test',
            'question_id': question, 'repetition': 1, 'complete': True,
            'report_complete': True, 'events': {'E': units,
                'P': [{'id': 'p0', 'judges': {'gemini': 0, 'gpt': 0}, 'evidence_pointer': 'trace'}],
                'A': [{'id': 'a0', 'judges': {'gemini': 0, 'gpt': 0}, 'executed': True, 'evidence_pointer': 'trace'}]},
            'report': {'judges': {'gemini': 0, 'gpt': 0}, 'evidence_pointer': 'report'}}


class PaperMetricsTests(unittest.TestCase):
    def test_finalized_csv_retains_zeros_and_clustered_three_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'finalized.csv'
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['model', 'framework', 'condition', 'question_id', 'repetition', *'EPAT'])
                writer.writeheader()
                for q in range(100):
                    for r in (1, 2, 3):
                        writer.writerow({'model': 'test', 'framework': 'test', 'condition': 'test', 'question_id': str(q), 'repetition': r,
                                         'E': 0, 'P': .25, 'A': r/10, 'T': q % 2})
            result = metrics.finalized_csv(path, 'fraction')['groups'][0]
            self.assertEqual(result['percent']['E'], 0)
            self.assertAlmostEqual(result['percent']['T'], 50)
            self.assertEqual(result['ci95_percent']['E'], [0, 0])
            self.assertAlmostEqual(result['run_sd_pp']['A'], 10)
            lines = path.read_text().splitlines()
            path.write_text('\n'.join(lines[:-1]))
            with self.assertRaisesRegex(ValueError, '100 questions'):
                metrics.finalized_csv(path, 'fraction')
    def test_trajectory_equal_not_event_pooled_or_any_hit(self):
        result = metrics.summarize([row('a', 1, 2), row('b', 1, 10)])
        self.assertAlmostEqual(result['groups'][0]['percent']['E'], 30)

    def test_duplicate_log_operation_counts_once(self):
        value = row('a', 1, 2)
        value['events']['E'].append(copy.deepcopy(value['events']['E'][0]))
        self.assertEqual(metrics.trajectory_scores(value)['E'], .5)

    def test_disagreement_requires_human(self):
        value = row('a', 1, 2)
        value['report']['judges']['gpt'] = 1
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)
        value['report'].update(human_label=1, adjudication_note='Reviewed complete report', quote='Endorsement')
        self.assertEqual(metrics.trajectory_scores(value)['T'], 1)

    def test_no_incomplete_report_or_implicit_missing_zero(self):
        value = row('a', 1, 2)
        value['report_complete'] = False
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)
        value['report_complete'] = True
        value['events']['P'] = []
        value['zero_event_scores'] = {'P': 0}
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)

    def test_unexecuted_action_is_not_positive(self):
        value = row('a', 1, 2)
        value['events']['P'] = [{'id': 'p', 'judges': {'gemini': 1, 'gpt': 1}, 'quote': 'plan', 'evidence_pointer': 'trace'}]
        value['events']['A'] = [{'id': 'a', 'judges': {'gemini': 1, 'gpt': 1}, 'quote': 'action', 'evidence_pointer': 'trace', 'plan_id': 'p', 'executed': False, 'after_plan': True}]
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)

    def test_unexecuted_negative_action_cannot_dilute_denominator(self):
        value = row('a', 1, 2)
        value['events']['A'] = [{'id': 'never-executed', 'judges': {'gemini': 0, 'gpt': 0}, 'executed': False, 'evidence_pointer': 'trace'}]
        with self.assertRaisesRegex(ValueError, 'denominator'):
            metrics.trajectory_scores(value)

    def test_strings_and_float_labels_not_treated_as_binary(self):
        for invalid in ('false', 0.0, 1.0, [], {}):
            value = row('a', 1, 2)
            value['events']['E'][0]['label'] = invalid
            with self.assertRaises(ValueError):
                metrics.trajectory_scores(value)
        value = row('a', 1, 2)
        value['complete'] = 'false'
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)

    def test_negative_units_still_need_auditable_operation_records(self):
        value = row('a', 0, 2)
        value['events']['E'][0].pop('evidence_pointer')
        with self.assertRaises(ValueError):
            metrics.trajectory_scores(value)


if __name__ == '__main__':
    unittest.main()
