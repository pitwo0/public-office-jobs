"""Offline publication boundary checks; no official requests."""
import copy
import json
from pathlib import Path
import unittest

from build import ROOT, validate_data
from deployment.public_data import public_data, runtime_data


class PublicDataTests(unittest.TestCase):
    def test_projection_preserves_facts_without_private_records(self):
        data = json.loads((ROOT / 'data/notices.json').read_text(encoding='utf-8'))
        before = copy.deepcopy(data)
        public = public_data(data, generated_at='2026-09-13T00:00:00+00:00')
        validate_data(public)
        self.assertEqual(data, before)
        self.assertEqual([n['id'] for n in public['notices']], [n['id'] for n in data['notices']])
        for left, right in zip(data['notices'], public['notices']):
            self.assertEqual(left['checkedAt'], right['checkedAt'])
            for old, new in zip(left['units'], right['units']):
                for field in ('vacancies', 'computer', 'english', 'pay', 'contract'):
                    self.assertEqual(old[field]['value'], new[field]['value'])
                    self.assertEqual(old[field]['status'], new[field]['status'])
                self.assertEqual(old.get('competition', {}).get('published') if old.get('competition') else None,
                                 new.get('competition', {}).get('published') if new.get('competition') else None)
        encoded = json.dumps(public, ensure_ascii=False)
        for forbidden in ('/workspace/', '"rawPath"', '"previousVerified"', '"lastRunSummary"', 'evidence/'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(public['publishedDataUrl'], 'data/notices.json')

    def test_runtime_keeps_resume_and_hashes_without_secrets(self):
        incoming = {'nextPage': 15, 'inbox': {'job-alio-123': {'row': {'idx': '123'}, 'lastSuccessAt': 'x'}},
                    'originBlocked': {'httpStatus': 403}, 'conditionsHash': 'abc',
                    'api_key': 'hidden', 'password': 'hidden', 'rawPath': '/workspace/a',
                    'note': 'error /tmp/private/file.json', 'url': 'https://example.org/?access_token=hidden'}
        result = runtime_data(incoming)
        self.assertEqual(result['nextPage'], 15)
        self.assertEqual(result['originBlocked']['httpStatus'], 403)
        self.assertEqual(result['conditionsHash'], 'abc')
        self.assertIn('job-alio-123', result['inbox'])
        self.assertNotIn('hidden', json.dumps(result))
        self.assertNotIn('/tmp/', result['note'])


if __name__ == '__main__':
    unittest.main()
