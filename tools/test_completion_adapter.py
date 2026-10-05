"""Checks that compatibility conversion does not add experiment instructions."""
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.dict(os.environ, FORGE_FRAMEWORK_DIR=self.tmp.name):
            self.translate = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'code/frameworks/webthinker/src/completion_adapter.py'))['translate']

    def payload(self, prefix=''):
        content = 'You are a research assistant with the ability to perform web searches. Original instructions.'
        return {'model': 'victim', 'prompt': '<|im_start|>user\n' + content + '<|im_end|>\n<|im_start|>assistant\n' + prefix}, content

    def test_no_added_prompt_even_with_obsolete_flags(self):
        payload, content = self.payload()
        request, _ = self.translate(payload, {'native_marker_contract': True, 'english_search_queries': True, 'wiki_snapshot_search_contract': True})
        self.assertEqual(request['messages'], [{'role': 'user', 'content': content}])

    def test_unverified_continuation_rejected(self):
        payload, _ = self.payload('Existing generated text')
        with self.assertRaisesRegex(ValueError, 'verified provider support'):
            self.translate(payload, {})

    def test_verified_prefix_preserved_without_replay_instruction(self):
        payload, content = self.payload('Existing generated text')
        request, _ = self.translate(payload, {'partial_continuation_verified': True})
        self.assertEqual(request['messages'], [{'role': 'user', 'content': content}, {'role': 'assistant', 'content': 'Existing generated text', 'partial': True}])


if __name__ == '__main__':
    unittest.main()
