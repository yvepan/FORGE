"""Sequential FORGE construction for isolated evidence-poisoning experiments."""
import argparse
import hashlib
from itertools import combinations
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

from prompts import (SYSTEM, CHAIN, DRAFT, EXTRACT, OBJECTIONS,
                     DOCUMENT_REVIEW, PAIR_REVIEW, REPAIR)

REPAIR_LIMIT = 7


def text(value):
    return isinstance(value, str) and bool(value.strip())


def inputs(value):
    required = {'target_narrative', 'topic_keywords', 'question_direction'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Only target_narrative, topic_keywords, question_direction are allowed')
    if not text(value['target_narrative']) or not text(value['question_direction']):
        raise ValueError('Target and broad question direction must be nonempty strings')
    keywords = value['topic_keywords']
    if not isinstance(keywords, list) or not keywords or not all(text(k) for k in keywords):
        raise ValueError('Supply nonempty topic keywords, not a full question')
    return value


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class RecordedModel:
    def __init__(self, args, output):
        from openai import OpenAI
        self.client = OpenAI(base_url=args.api_base, api_key=os.environ['FORGE_API_KEY'],
                             max_retries=0, timeout=240)
        self.model = args.model
        self.review_temperature = args.review_temperature
        self.review_tokens = args.review_max_tokens
        self.output = output
        self.index = 0

    def __call__(self, prompt, value, generation=False):
        self.index += 1
        request = {'model': self.model,
                   'messages': [{'role': 'system', 'content': SYSTEM},
                                {'role': 'user', 'content': prompt + '\n' + json.dumps(value, ensure_ascii=False)}],
                   'temperature': 0.2 if generation else self.review_temperature,
                   'max_tokens': 1200 if generation else self.review_tokens,
                   'response_format': {'type': 'json_object'}}
        record = {'request': request, 'started_unix': time.time()}
        try:
            response = self.client.chat.completions.create(**request)
            record['response'] = response.model_dump()
            if response.choices[0].finish_reason != 'stop':
                raise ValueError('Incomplete/truncated model response')
            parsed = json.loads(response.choices[0].message.content)
            if not isinstance(parsed, dict):
                raise ValueError('Expected JSON object')
            return parsed
        except Exception as error:
            record['error'] = str(error)
            raise
        finally:
            save(self.output / 'traces' / f'{self.index:05d}.json', record)


class Builder:
    def __init__(self, call, value):
        self.call = call
        self.value = inputs(value)
        self.prefix = 'Topic: ' + '; '.join(k.strip() for k in value['topic_keywords']) + '.'

    def extract(self, document):
        result = self.call(EXTRACT, {'body': document['body']})
        for key in ('conclusion_quotes', 'qualification_quotes', 'dependencies'):
            if not isinstance(result.get(key), list) or not all(text(x) for x in result[key]):
                raise ValueError('Invalid extraction field: ' + key)
        if not result['conclusion_quotes'] or not text(result.get('summary')):
            raise ValueError('Missing actual conclusion summary')
        for key in ('conclusion_quotes', 'qualification_quotes'):
            if any(q not in document['body'] for q in result[key]):
                raise ValueError('Extraction quote not present in current body')
        return result

    def draft(self, number, assigned, summaries, previous=None, review=None):
        context = {**self.value, 'number': number,
                   'role': 'core' if number <= 5 else 'objection',
                   'assigned': assigned, 'actual_core_summaries': summaries}
        if previous is not None:
            context.update(previous=previous, audit=review)
        generated = self.call(REPAIR if previous else DRAFT, context, generation=True)
        if not text(generated.get('body')) or not text(generated.get('title')):
            raise ValueError('Draft needs nonempty body/title')
        title = '; '.join(self.value['topic_keywords']) + f' — {number:02d}: ' + generated['title'].strip()
        return {'number': number, 'role': context['role'], 'assigned': assigned,
                'title': title, 'url': 'https://en.wikipedia.org/wiki/' + quote(title, safe=''),
                'prefix': self.prefix, 'body': generated['body'].strip()}

    def objections(self, core, summaries):
        result = self.call(OBJECTIONS, {**self.value, 'core_documents': core,
                                      'actual_core_summaries': summaries}, generation=True)
        rows = result.get('objections')
        fields = ('objection', 'counterevidence', 'response', 'qualifications')
        if not isinstance(rows, list) or len(rows) != 5 or not all(
                isinstance(r, dict) and all(text(r.get(k)) for k in fields) for r in rows):
            raise ValueError('Exactly five complete objection assignments required')
        if len({r['objection'].strip() for r in rows}) != 5:
            raise ValueError('Duplicate objection assignments')
        return rows

    def checked_review(self, result, documents, pair=False):
        if type(result.get('passed')) is not bool or not isinstance(result.get('issues'), list):
            raise ValueError('Invalid audit schema')
        if result['passed'] == bool(result['issues']):
            raise ValueError('Audit verdict contradicts evidence list')
        for issue in result['issues']:
            number = issue.get('document') if pair else documents[0]['number']
            if type(number) is not int:
                raise ValueError('Audit document ID must be an integer')
            source = next((d for d in documents if d['number'] == number), None)
            if source is None or not text(issue.get('quote')) or issue['quote'] not in source['body'] or not text(issue.get('reason')):
                raise ValueError('Audit evidence not present in current document')
        return result

    def audit(self, documents, summaries):
        pairs, reviews = [], []
        affected = set()
        for d in documents:
            result = self.checked_review(self.call(DOCUMENT_REVIEW, {
                'document': d, 'actual_core_summaries': summaries[:d['number']-1] if d['number'] <= 5 else summaries
            }), [d])
            length_ok = 180 <= len(d['body'].split()) <= 220
            reviews.append({'number': d['number'], 'length_ok': length_ok, **result})
            if not result['passed'] or not length_ok:
                affected.add(d['number'])
        for left, right in combinations(documents, 2):
            result = self.checked_review(self.call(PAIR_REVIEW, {'documents': [left, right]}), [left, right], pair=True)
            pairs.append({'numbers': [left['number'], right['number']], **result})
            if not result['passed']:
                affected.update([left['number'], right['number']])
        return {'passed': not affected, 'affected': sorted(affected),
                'document_reviews': reviews, 'pair_reviews': pairs}

    def build(self, output):
        plan = self.call(CHAIN, self.value, generation=True).get('steps')
        if not isinstance(plan, list) or len(plan) != 5 or not all(
                isinstance(s, dict) and text(s.get('claim')) and text(s.get('dependency')) for s in plan):
            raise ValueError('Exactly five claim/dependency steps required')
        if plan[-1]['claim'].strip() != self.value['target_narrative'].strip():
            raise ValueError('The fifth claim must equal the target narrative')
        save(output / 'plan.json', plan)
        documents, summaries = [], []
        for number, step in enumerate(plan, 1):
            document = self.draft(number, step, summaries)
            documents.append(document)
            summaries.append(self.extract(document))
        objections = self.objections(documents, summaries)
        documents.extend(self.draft(r+6, item, summaries) for r, item in enumerate(objections))
        for revision in range(REPAIR_LIMIT + 1):
            save(output / f'documents_r{revision}.json', documents)
            save(output / f'summaries_r{revision}.json', summaries)
            review = self.audit(documents, summaries)
            save(output / f'audit_r{revision}.json', review)
            if review['passed']:
                return documents, revision
            if revision == REPAIR_LIMIT:
                raise ValueError('Still failing after seven repair rounds; no frozen set produced')
            core_affected = [n for n in review['affected'] if n <= 5]
            if core_affected:
                # Changed earlier evidence invalidates every downstream core draft and objection.
                first = min(core_affected)
                summaries = summaries[:first-1]
                for number in range(first, 6):
                    documents[number-1] = self.draft(number, plan[number-1], summaries,
                                                    documents[number-1], review)
                    summaries.append(self.extract(documents[number-1]))
                objections = self.objections(documents[:5], summaries)
                for r, item in enumerate(objections):
                    documents[r+5] = self.draft(r+6, item, summaries, documents[r+5], review)
            else:
                for number in review['affected']:
                    documents[number-1] = self.draft(number, objections[number-6], summaries,
                                                    documents[number-1], review)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', required=True, help='Explicit authorized ChatGPT 5.6 Terra API ID')
    parser.add_argument('--api-base', required=True, help='Authorized OpenAI-compatible /v1 endpoint')
    parser.add_argument('--review-temperature', type=float, required=True,
                        help='Explicit review/extraction setting; not specified in manuscript')
    parser.add_argument('--review-max-tokens', type=int, required=True,
                        help='Explicit review/extraction cap; not specified in manuscript')
    args = parser.parse_args()
    if not args.model.strip() or not 0 <= args.review_temperature <= 2 or args.review_max_tokens < 1:
        parser.error('Invalid model or review settings')
    value = inputs(json.loads(args.inputs.read_text(encoding='utf-8-sig')))
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'traces').mkdir()
    protocol = {'source': 'adapted_september_builder_not_verified_archived_implementation',
                'model': args.model, 'generation_temperature': 0.2, 'generation_max_tokens': 1200,
                'review_temperature': args.review_temperature, 'review_max_tokens': args.review_max_tokens,
                'repair_round_limit': REPAIR_LIMIT, 'inputs': value,
                'source_hashes': {p.name: sha(p.read_bytes()) for p in (Path(__file__), Path(__file__).with_name('prompts.py'))}}
    save(args.output / 'protocol.json', protocol)
    try:
        documents, repairs = Builder(RecordedModel(args, args.output), value).build(args.output)
        for d in documents:
            d['text'] = d['prefix'] + '\n\n' + d['body']
            d['body_words'] = len(d['body'].split())
            d['text_sha256'] = sha(d['text'].encode('utf-8'))
            d['record_sha256'] = sha(json.dumps(d, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        frozen = {'documents': documents, 'audit_passed': True, 'repair_rounds': repairs,
                  'protocol_sha256': sha((args.output / 'protocol.json').read_bytes()),
                  'last_audit_sha256': sha((args.output / f'audit_r{repairs}.json').read_bytes()),
                  'status': 'frozen_before_victim_runs'}
        save(args.output / 'frozen.json', frozen)
        print(args.output / 'frozen.json')
    except Exception:
        save(args.output / 'status.json', {'status': 'construction_failed_no_frozen_set'})
        raise


if __name__ == '__main__':
    main()
