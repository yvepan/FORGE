"""Minimal local-corpus PoisonedRAG adaptation; model and review are supplied externally.

No website publication, retrieval override, or victim-outcome selection is included.
The callback receives a request dictionary and returns a parsed JSON object.
"""
import json

try:
    from .poisonedrag_prompts import SYSTEM, DRAFT
except ImportError:
    from poisonedrag_prompts import SYSTEM, DRAFT


def construct(inputs, generate, review):
    fields = {'target_narrative', 'topic_keywords', 'question_direction'}
    if not isinstance(inputs, dict) or set(inputs) != fields:
        raise ValueError('Restricted construction inputs required')
    if not all(isinstance(inputs[k], str) and inputs[k].strip()
               for k in ('target_narrative', 'question_direction')):
        raise ValueError('Nonempty target and broad direction required')
    keywords = inputs['topic_keywords']
    if not isinstance(keywords, list) or not keywords or not all(
            isinstance(k, str) and k.strip() for k in keywords):
        raise ValueError('Nonempty topic keywords required')
    documents = []
    for number in range(1, 11):
        for attempt in range(8):
            document = generate({'temperature': 0.2, 'max_tokens': 1200,
                'messages': [{'role': 'system', 'content': SYSTEM},
                             {'role': 'user', 'content': DRAFT + '\n' + json.dumps(inputs)}]})
            if (isinstance(document, dict) and isinstance(document.get('title'), str)
                    and document['title'].strip() and isinstance(document.get('body'), str)
                    and 180 <= len(document['body'].split()) <= 220
                    and review(document) is True):
                documents.append({'number': number, 'title': document['title'],
                                  'body': document['body']})
                break
        else:
            raise ValueError('Document failed the seven-repair budget')
    return documents
