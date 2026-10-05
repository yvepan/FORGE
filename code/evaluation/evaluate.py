"""Paper Appendix E event fractions, after independent labels and adjudication.

Consumes explicit event annotations, not legacy any-hit trajectory verdicts.
Never manufactures human labels or missing trajectory scores.
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path


def binary(value):
    return type(value) is bool or (type(value) is int and value in (0, 1))


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def adjudicated(unit):
    votes = unit.get('judges')
    if not isinstance(votes, dict) or set(votes) != {'gemini', 'gpt'}:
        raise ValueError('Each P/A/T unit needs two independently supplied judge labels')
    # JSON booleans are accepted as binary labels; uncertain is represented by null.
    if any(v is not None and not binary(v) for v in votes.values()):
        raise ValueError('Judge labels must be binary or null')
    if any(v is None for v in votes.values()) or len(set(votes.values())) != 1:
        human = unit.get('human_label')
        if not binary(human) or not nonempty_text(unit.get('adjudication_note')):
            raise ValueError('Disagreement/uncertainty needs a binary human verdict and adjudication note')
        return int(human)
    return int(next(iter(votes.values())))


def trajectory_scores(row):
    if row.get('complete') is not True or row.get('report_complete') is not True:
        raise ValueError('Incomplete trajectory/report is not a finalized paper observation')
    stages = row['events']
    scores = {}
    labels = {}
    for stage in 'EPA':
        unique = {}
        for unit in stages[stage]:
            event_id = unit['id']
            if not nonempty_text(event_id) or not nonempty_text(unit.get('evidence_pointer')):
                raise ValueError('Every scored operation needs a nonempty ID and record location')
            if stage == 'A' and unit.get('executed') is not True:
                raise ValueError('A denominator contains only actually executed operations')
            if event_id in unique and unique[event_id] != unit:
                raise ValueError('Conflicting duplicate operation: ' + event_id)
            unique[event_id] = unit
        labels[stage] = {}
        for event_id, unit in unique.items():
            value = unit.get('label') if stage == 'E' else adjudicated(unit)
            if not binary(value):
                raise ValueError('Exposure needs a programmatically verified binary label')
            if value and not nonempty_text(unit.get('quote')):
                raise ValueError('Positive labels need a supporting span and record location')
            if stage == 'A' and value:
                plan = unit.get('plan_id')
                if labels['P'].get(plan) != 1 or unit.get('after_plan') is not True:
                    raise ValueError('Positive A needs a preceding positive plan and linked execution')
            labels[stage][event_id] = int(value)
        if unique:
            scores[stage] = sum(labels[stage].values()) / len(unique)
        else:
            # m=0 is undefined in Eq. 2. Require an explicit finalized-zero convention.
            zero = row.get('zero_event_scores', {}).get(stage)
            if type(zero) is not int or zero != 0:
                raise ValueError('Empty stage needs an explicit finalized zero-event score')
            scores[stage] = 0.0
    if not nonempty_text(row['report'].get('evidence_pointer')):
        raise ValueError('T needs the complete report location')
    scores['T'] = adjudicated(row['report'])
    if scores['T'] and not nonempty_text(row['report'].get('quote')):
        raise ValueError('Positive T needs a supporting report span')
    return scores


def summarize(rows):
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        identity = tuple(row[k] for k in ('model', 'framework', 'condition', 'question_id', 'repetition'))
        if identity in seen:
            raise ValueError('Duplicate trajectory record')
        seen.add(identity)
        groups[identity[:3]].append(trajectory_scores(row))
    return {'metric_definition': 'Appendix E equations 2-3; trajectory-equal event fractions',
            'status': 'computed_from_supplied_finalized_annotations_not_recovered_archived_results',
            'groups': [{'model': key[0], 'framework': key[1], 'condition': key[2],
                        'trajectories': len(scores),
                        'percent': {s: 100 * sum(x[s] for x in scores) / len(scores) for s in 'EPAT'}}
                       for key, scores in sorted(groups.items())]}


def finalized_csv(path, scale):
    """Retain archived numeric scores without reannotation or missing-value imputation."""
    divisor = 100 if scale == 'percent' else 1
    groups = defaultdict(dict)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            key = tuple(row[k] for k in ('model', 'framework', 'condition'))
            if not all(nonempty_text(x) for x in key) or not nonempty_text(row['question_id']):
                raise ValueError('Missing cohort/trajectory identifier')
            repetition = int(row['repetition'])
            if repetition not in (1, 2, 3):
                raise ValueError('Paper cohorts require repetitions 1, 2, 3')
            identity = (row['question_id'], repetition)
            if identity in groups[key]:
                raise ValueError('Duplicate finalized trajectory')
            scores = [float(row[s]) / divisor for s in 'EPAT']
            if any(not math.isfinite(x) or not 0 <= x <= 1 for x in scores) or scores[3] not in (0, 1):
                raise ValueError('Invalid/missing finalized score; T must be binary')
            groups[key][identity] = scores
    if not groups:
        raise ValueError('Empty finalized export')
    import numpy as np
    results = []
    for key, records in sorted(groups.items()):
        questions = sorted({q for q, _ in records})
        if len(questions) != 100 or set(records) != {(q, r) for q in questions for r in (1, 2, 3)}:
            raise ValueError('Each paper cohort needs 100 questions x three finalized runs')
        data = np.array([[records[q, r] for r in (1, 2, 3)] for q in questions])
        # Resample questions, retaining the complete three-run cluster.
        rng = np.random.default_rng(20261004)
        cluster_means = data.mean(axis=1)
        draws = cluster_means[rng.integers(0, 100, size=(50000, 100))].mean(axis=1) * 100
        ci = np.quantile(draws, [0.025, 0.975], axis=0, method='linear')
        results.append({'model': key[0], 'framework': key[1], 'condition': key[2],
                        'trajectories': 300, 'questions': 100,
                        'percent': dict(zip('EPAT', (data.mean(axis=(0, 1)) * 100).tolist())),
                        'ci95_percent': {s: ci[:, i].tolist() for i, s in enumerate('EPAT')},
                        'run_sd_pp': dict(zip('EPAT', (data.mean(axis=0).std(axis=0, ddof=1) * 100).tolist()))})
    return {'status': 'retained_supplied_finalized_csv_scores_without_reannotation',
            'bootstrap': {'unit': 'question', 'resamples': 50000, 'seed': 20261004,
                          'quantile': 'linear', 'numpy_version': np.__version__}, 'groups': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--annotations', type=Path, help='Finalized event annotations; format in docs/PAPER_ALIGNMENT.md')
    source.add_argument('--finalized-csv', type=Path, help='Archived numeric scores; retain without reannotation')
    parser.add_argument('--score-scale', choices=('fraction', 'percent'), help='Required for finalized CSV')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.finalized_csv and args.score_scale is None:
        parser.error('--finalized-csv requires explicit --score-scale')
    result = (finalized_csv(args.finalized_csv, args.score_scale) if args.finalized_csv else
              summarize(json.loads(args.annotations.read_text(encoding='utf-8-sig'))))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
