import json
import math
import unittest
from datetime import datetime

import fetch_data as f


class ArtifactTests(unittest.TestCase):
    def test_data_contract(self):
        data = json.loads((f.ROOT/'data.json').read_text(encoding='utf-8'))
        self.assertEqual(set(data), {'generatedAt', 'method', 'metrics'})
        self.assertIsNotNone(datetime.fromisoformat(data['generatedAt']).tzinfo)
        self.assertEqual([m['id'] for m in data['metrics']], [s[0] for s in f.SPECS])
        required = {'id', 'section', 'title', 'unit', 'points', 'date', 'value', 'delta',
                    'period', 'note', 'formula', 'status', 'sources', 'secondary'}
        source_keys = {'id', 'url', 'observed', 'retrieved', 'origin', 'frequency', 'age',
                       'maxAge', 'status', 'fallback'}
        for m in data['metrics']:
            self.assertEqual(set(m), required)
            self.assertIn(m['status'], ['ok', 'stale', 'missing'])
            for source in m['sources']:
                self.assertEqual(set(source), source_keys)
            for points in [m['points']] + ([m['secondary']['points']] if m['secondary'] else []):
                dates = [d for d, _ in points]
                self.assertEqual(dates, sorted(set(dates)))
                for _, value in points:
                    self.assertTrue(value is None or math.isfinite(value))
            usable = [p for p in m['points'] if p[1] is not None]
            if usable:
                self.assertEqual([m['date'], m['value']], usable[-1])

    def test_async_loading_and_error_state(self):
        html = (f.ROOT/'index.html').read_text(encoding='utf-8')
        self.assertIn("fetch('./data.json'", html)
        self.assertNotIn('const DATA={', html)
        self.assertIn('if (!response.ok)', html)
        self.assertIn('})().catch(error =>', html)


if __name__ == '__main__':
    unittest.main()
