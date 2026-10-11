import json
import unittest
from dataclasses import replace
from pathlib import Path

from mmar import Decision, Limits
from mmar.protocol import InvalidDecision


class SharedProtocolTests(unittest.TestCase):
    def test_raw_decision_vectors(self):
        path = Path(__file__).resolve().parents[1] / 'spec' / 'decision-vectors.json'
        suite = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(suite['version'], 1)
        self.assertEqual(len({case['name'] for case in suite['cases']}), len(suite['cases']))
        for case in suite['cases']:
            with self.subTest(case=case['name']):
                limits = replace(Limits(), **case.get('limits', {}))
                if 'expected_error' in case:
                    with self.assertRaises(InvalidDecision) as raised:
                        Decision.parse(case['raw'], limits)
                    self.assertEqual(raised.exception.code, case['expected_error'])
                else:
                    self.assertEqual(Decision.parse(case['raw'], limits).to_dict(), case['expected'])
