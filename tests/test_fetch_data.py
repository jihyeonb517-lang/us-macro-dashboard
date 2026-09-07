import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import fetch_data as f


def raw(**series):
    return {sid: {'points': points, 'retrieved': '2026-09-07T00:00:00+00:00', 'error': None}
            for sid, points in series.items()}


class FormulaTests(unittest.TestCase):
    def test_calendar_lags_do_not_skip_missing_months(self):
        data = raw(CPILFESL=[['2025-01-01', 100], ['2025-03-01', 110],
                             ['2026-01-01', 105], ['2026-02-01', 106]])
        points = dict(f.calculate(data)['cpi'])
        self.assertAlmostEqual(points['2026-01-01'], 5)
        self.assertIsNone(points['2026-02-01'])

    def test_jobs_exact_three_month_difference(self):
        data = raw(PAYEMS=[['2026-01-01', 100], ['2026-04-01', 130]])
        self.assertEqual(f.calculate(data)['jobs'][-1][1], 10)

    def test_pce_yoy_and_annualization(self):
        data = raw(PCEPILFE=[['2025-01-01', 100], ['2025-10-01', 104], ['2026-01-01', 108]])
        result = f.calculate(data)
        self.assertAlmostEqual(result['pce'][-1][1], 8)
        self.assertAlmostEqual(result['pce_secondary'][-1][1], ((108/104)**4-1)*100)

    def test_claims_require_four_consecutive_weeks(self):
        days = ['2026-08-01', '2026-08-08', '2026-08-15', '2026-08-22']
        data = raw(ICSA=[[d, v] for d, v in zip(days, [200000, 220000, 240000, 260000])])
        self.assertEqual(f.calculate(data)['claims'][-1][1], 230)
        data['ICSA']['points'].pop(1)
        self.assertIsNone(f.calculate(data)['claims'][-1][1])

    def test_netliq_no_fill_or_future_input(self):
        data = raw(WALCL=[['2026-08-26', 8000000], ['2026-09-02', 8100000]],
                   WTREGEN=[['2026-08-26', 500000], ['2026-09-03', 600000]],
                   RRPONTSYD=[['2026-08-26', 200], ['2026-09-01', 210]])
        self.assertEqual(f.calculate(data)['netliq'], [['2026-08-26', 7300], ['2026-09-02', None]])

    def test_units_and_same_date_spreads(self):
        d = '2026-09-01'
        data = raw(WRESBAL=[[d, 3000000]], SOFR=[[d, 5.4]], IORB=[[d, 5.3]],
                   BAMLH0A0HYM2=[[d, 3.2]], DGS10=[[d, 4.2]], DGS2=[[d, 3.8]],
                   VIXCLS=[[d, 18]], DFII10=[[d, 1.8]], UNRATE=[[d, 4.1]])
        result = f.calculate(data)
        for mid, expected in [('reserves', 3000), ('repo', 10), ('credit', 320),
                              ('curve', .4), ('vix', 18), ('real', 1.8), ('unemployment', 4.1)]:
            self.assertAlmostEqual(result[mid][-1][1], expected)
        data['DGS2']['points'] = [['2026-09-02', 3.8]]
        self.assertIsNone(f.calculate(data)['curve'][-1][1])

    def test_market_rolling_windows_and_warmup(self):
        days = [(date(2025, 1, 1)+timedelta(days=i)).isoformat() for i in range(201)]
        data = raw(RSP=[[d, 100] for d in days], SPY=[[d, 200] for d in days])
        data['^GSPC'] = {'points': [[d, 100 if i < 200 else 200] for i, d in enumerate(days)]}
        result = f.calculate(data)
        self.assertIsNone(result['trend'][198][1])
        self.assertEqual(result['trend'][199][1], 0)
        self.assertAlmostEqual(result['trend'][200][1], (200/100.5-1)*100)
        self.assertIsNone(result['participation'][123][1])
        self.assertEqual(result['participation'][124][1], 0)

    def test_m2_and_missing_values(self):
        data = raw(M2SL=[['2025-01-01', 100], ['2026-01-01', 110]])
        self.assertAlmostEqual(f.calculate(data)['m2'][-1][1], 10)
        self.assertEqual(f.clean([['1990-01-01', '1'], ['1990-01-02', '.'],
                                  ['1990-01-03', 'nan'], ['2999-01-01', 5]]),
                         [['1990-01-01', 1.0], ['1990-01-02', None], ['1990-01-03', None]])


class FallbackTests(unittest.TestCase):
    def test_provider_timeout_returns_fallback(self):
        with patch.object(f.subprocess, 'run', side_effect=f.subprocess.TimeoutExpired('worker', 40)):
            sid, entry, error = f.fetch_bounded('UNRATE')
        self.assertEqual(sid, 'UNRATE')
        self.assertIsNone(entry)
        self.assertIn('40s deadline', error)

    def test_last_value_retained_without_filling_points(self):
        data = raw(UNRATE=[['2026-07-01', 4.2], ['2026-08-01', None]])
        metric = f.build(data, {}, None, date(2026, 9, 7))['metrics'][1]
        self.assertEqual((metric['date'], metric['value'], metric['status']), ('2026-07-01', 4.2, 'stale'))
        self.assertIsNone(metric['points'][-1][1])
        self.assertTrue(metric['sources'][0]['fallback'])

    def test_failed_dependency_preserves_metric_and_provenance(self):
        data = raw(UNRATE=[['2026-08-01', 4.2]])
        previous = f.build(data, {}, '2026-09-06T00:00:00+00:00', date(2026, 9, 7))
        data['UNRATE']['error'] = 'timeout'
        data['UNRATE']['points'] = []
        result = f.build(data, previous, previous['generatedAt'], date(2026, 9, 8))
        m = result['metrics'][1]
        self.assertEqual(m['value'], 4.2)
        self.assertEqual(m['status'], 'stale')
        self.assertTrue(m['sources'][0]['fallback'])
        self.assertEqual(m['sources'][0]['retrieved'], previous['metrics'][1]['sources'][0]['retrieved'])

    def test_total_failure_keeps_generation_time(self):
        with tempfile.TemporaryDirectory() as directory:
            output, cache = Path(directory)/'data.json', Path(directory)/'raw.json'
            data = raw(UNRATE=[['2026-08-01', 4.2]])
            timestamp = '2026-09-06T00:00:00+00:00'
            f.atomic_json(cache, data)
            f.atomic_json(output, f.build(data, {}, timestamp))
            with patch.object(f, 'fetch_bounded', side_effect=lambda sid: (sid, None, 'timeout')):
                self.assertEqual(f.refresh(output, cache), 0)
            result = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(result['generatedAt'], timestamp)
            self.assertEqual(result['metrics'][1]['value'], 4.2)

    def test_stale_daily_source_and_empty_bootstrap(self):
        data = raw(VIXCLS=[['2026-01-01', 18]])
        result = f.build(data, {}, None, date(2026, 9, 7))
        self.assertEqual(result['metrics'][11]['status'], 'stale')
        self.assertTrue(result['metrics'][11]['sources'][0]['fallback'])
        self.assertEqual(result['metrics'][0]['status'], 'missing')


if __name__ == '__main__':
    unittest.main()
