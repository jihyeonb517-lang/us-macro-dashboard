"""Refresh dashboard observations. No API keys, interpolation, or forward filling.

Run: python fetch_data.py. --offline recomputes from the local observation cache.
Dates are observation dates, NOT historical release/vintage dates.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FREQUENCIES = {
    'PAYEMS': 'monthly', 'UNRATE': 'monthly', 'PCEPILFE': 'monthly',
    'CPILFESL': 'monthly', 'ICSA': 'weekly', 'WALCL': 'weekly',
    'WTREGEN': 'weekly', 'WRESBAL': 'weekly', 'RRPONTSYD': 'daily',
    'M2SL': 'monthly', 'SOFR': 'daily', 'IORB': 'daily',
    'BAMLH0A0HYM2': 'daily', 'VIXCLS': 'daily', 'DFII10': 'daily',
    'DGS10': 'daily', 'DGS2': 'daily', '^GSPC': 'daily',
    'RSP': 'daily', 'SPY': 'daily',
}
MAX_AGE = {'daily': 7, 'weekly': 18, 'monthly': 75}
YAHOO = {'^GSPC', 'RSP', 'SPY'}
# id, section, title, unit, dependencies, delta lag, comparison label
SPECS = [
    ('jobs', 'economy', '비농업 고용 증가', '천 명', ['PAYEMS'], 3, '직전 3개월 평균 대비'),
    ('unemployment', 'economy', '실업률', '%', ['UNRATE'], 3, '3개월 전 대비'),
    ('pce', 'economy', '근원 PCE 상승률', '%', ['PCEPILFE'], 3, '3개월 전 대비'),
    ('cpi', 'economy', '근원 CPI 상승률', '%', ['CPILFESL'], 3, '3개월 전 대비'),
    ('claims', 'economy', '신규 실업수당 청구', '천 건', ['ICSA'], 4, '4주 전 대비'),
    ('netliq', 'conditions', '연준 순유동성 참고치', '십억 달러', ['WALCL', 'WTREGEN', 'RRPONTSYD'], 4, '4주 전 대비'),
    ('reserves', 'conditions', '은행 지준금', '십억 달러', ['WRESBAL'], 4, '4주 전 대비'),
    ('m2', 'conditions', 'M2 증가율', '%', ['M2SL'], 3, '3개월 전 대비'),
    ('repo', 'conditions', 'SOFR − IORB', 'bp', ['SOFR', 'IORB'], 20, '20관측일 전 대비'),
    ('credit', 'conditions', '하이일드 신용 스프레드', 'bp', ['BAMLH0A0HYM2'], 20, '20관측일 전 대비'),
    ('trend', 'market', 'S&P 500 · 200일선 이격', '%', ['^GSPC'], 20, '20거래일 전 대비'),
    ('vix', 'market', 'VIX', '지수', ['VIXCLS'], 20, '20관측일 전 대비'),
    ('participation', 'market', '동일가중 상대강도', '%', ['RSP', 'SPY'], 20, '20거래일 전 대비'),
    ('real', 'market', '미국 10년 실질금리', '%', ['DFII10'], 20, '20관측일 전 대비'),
    ('curve', 'market', '미국 10년 − 2년 금리', '%p', ['DGS10', 'DGS2'], 20, '20관측일 전 대비'),
]
FORMULAS = {
    'jobs': '(PAYEMS[t] − PAYEMS[t−3 calendar months]) / 3; thousands',
    'unemployment': 'UNRATE (%)',
    'pce': '(PCEPILFE[t]/PCEPILFE[t−12 months]−1)×100; secondary: ((PCEPILFE[t]/PCEPILFE[t−3 months])^4−1)×100',
    'cpi': '(CPILFESL[t]/CPILFESL[t−12 months]−1)×100; no interpolation',
    'claims': 'Mean of 4 consecutive calendar weeks of ICSA / 1000',
    'netliq': 'WALCL/1000 − WTREGEN/1000 − RRPONTSYD; exact same observation date, no forward fill',
    'reserves': 'WRESBAL (millions) / 1000 = billions',
    'm2': '(M2SL[t]/M2SL[t−12 months]−1)×100',
    'repo': '(SOFR−IORB)×100; exact same observation date; bp',
    'credit': 'BAMLH0A0HYM2 (%) × 100 = bp',
    'trend': '(Yahoo ^GSPC Close / 200-session mean Close−1)×100',
    'vix': 'FRED VIXCLS closing value',
    'participation': '((RSP Close/SPY Close)/125-common-session mean of ratio−1)×100; auto_adjust=False',
    'real': 'FRED DFII10 (%)',
    'curve': 'DGS10−DGS2; exact same observation date; percentage points',
}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def clean(rows, today=None):
    """Preserve explicit gaps and all available history; reject future dates."""
    today = today or datetime.now(timezone.utc).date()
    result = {}
    for day, value in rows:
        try:
            day = str(day)[:10]
            if date.fromisoformat(day) > today:
                continue
        except (ValueError, TypeError):
            continue
        try:
            value = float(value)
            value = value if math.isfinite(value) else None
        except (ValueError, TypeError):
            value = None
        result[day] = value
    return [[d, v] for d, v in sorted(result.items())]


def fetch_series(sid):
    if sid in YAHOO:
        import yfinance as yf
        frame = yf.download(sid, period='max', interval='1d', auto_adjust=False,
                            back_adjust=False, repair=False, keepna=True,
                            progress=False, threads=False, timeout=30,
                            multi_level_index=False)
        if frame is None or frame.empty:
            raise ValueError('Yahoo returned no prices')
        # Exclude today's potentially incomplete daily candle.
        today_ny = datetime.now(ZoneInfo('America/New_York')).date()
        points = clean(((d.strftime('%Y-%m-%d'), v) for d, v in frame['Close'].items()),
                       today=today_ny - timedelta(days=1))
    else:
        url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}'
        with urlopen(Request(url, headers={'User-Agent': 'MacroDashboard/1.0'}), timeout=35) as response:
            rows = list(csv.reader(io.StringIO(response.read().decode('utf-8-sig'))))
        if not rows or len(rows[0]) != 2 or rows[0][1] != sid:
            raise ValueError('Unexpected FRED CSV header')
        points = clean(row for row in rows[1:] if len(row) == 2)
    if not any(finite(v) for _, v in points):
        raise ValueError('No finite observations')
    return {'points': points, 'retrieved': utcnow(), 'error': None}


def fetch_retry(sid):
    for attempt in range(3):
        try:
            return sid, fetch_series(sid), None
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if attempt < 2:
                time.sleep(2 ** attempt)
    return sid, None, error


def calendar(points, weekly=False):
    """Insert null calendar slots, never invented values."""
    if not points:
        return []
    mapped = {d if weekly else d[:7] + '-01': v for d, v in points}
    current, end = date.fromisoformat(min(mapped)), date.fromisoformat(max(mapped))
    result = []
    while current <= end:
        result.append([current.isoformat(), mapped.get(current.isoformat())])
        current = (current + timedelta(days=7) if weekly else
                   date(current.year + (current.month == 12), current.month % 12 + 1, 1))
    return result


def transform(points, fn):
    return [[d, fn(v) if finite(v) else None] for d, v in points]


def lagged(points, lag, fn):
    return [[d, fn(v, points[i-lag][1]) if i >= lag and finite(v)
             and finite(points[i-lag][1]) else None] for i, (d, v) in enumerate(points)]


def average(points, count):
    output = []
    for i, (d, _) in enumerate(points):
        values = [v for _, v in points[max(0, i-count+1):i+1]]
        output.append([d, sum(values)/count if len(values) == count
                       and all(finite(v) for v in values) else None])
    return output


def aligned(series, fn):
    """Anchor to first series; missing same-date inputs stay null."""
    if not series:
        return []
    maps = [dict(points) for points in series]
    result = []
    for d, _ in series[0]:
        values = [m.get(d) for m in maps]
        result.append([d, fn(*values) if all(finite(v) for v in values) else None])
    return result


def calculate(raw):
    s = lambda sid: raw.get(sid, {}).get('points', [])
    m = lambda sid: calendar(s(sid))
    yoy = lambda sid: lagged(m(sid), 12, lambda a, b: (a/b-1)*100 if b > 0 else None)
    ratio = aligned([s('RSP'), s('SPY')], lambda a, b: a/b if b > 0 else None)
    # Keep only common trading dates, retaining explicit nulls on common dates.
    spy_dates = dict(s('SPY'))
    ratio = [p for p in ratio if p[0] in spy_dates]
    return {
        'jobs': transform(lagged(m('PAYEMS'), 3, lambda a, b: a-b), lambda v: v/3),
        'unemployment': m('UNRATE'), 'pce': yoy('PCEPILFE'), 'cpi': yoy('CPILFESL'),
        'claims': transform(average(calendar(s('ICSA'), weekly=True), 4), lambda v: v/1000),
        'netliq': aligned([s('WALCL'), s('WTREGEN'), s('RRPONTSYD')], lambda a, t, r: a/1000-t/1000-r),
        'reserves': transform(calendar(s('WRESBAL'), weekly=True), lambda v: v/1000),
        'm2': yoy('M2SL'),
        'repo': aligned([s('SOFR'), s('IORB')], lambda a, b: (a-b)*100),
        'credit': transform(s('BAMLH0A0HYM2'), lambda v: v*100),
        'trend': aligned([s('^GSPC'), average(s('^GSPC'), 200)], lambda v, ma: (v/ma-1)*100 if ma > 0 else None),
        'vix': s('VIXCLS'),
        'participation': aligned([ratio, average(ratio, 125)], lambda v, ma: (v/ma-1)*100 if ma > 0 else None),
        'real': s('DFII10'), 'curve': aligned([s('DGS10'), s('DGS2')], lambda a, b: a-b),
        'pce_secondary': lagged(m('PCEPILFE'), 3, lambda a, b: ((a/b)**4-1)*100 if b > 0 else None),
    }


def source_metadata(sid, raw, today):
    entry = raw.get(sid, {})
    points = entry.get('points', [])
    usable = [p for p in points if finite(p[1])]
    observed = usable[-1][0] if usable else None
    frequency = FREQUENCIES[sid]
    age = (today-date.fromisoformat(observed)).days if observed else None
    stale = bool(observed and (age > MAX_AGE[frequency] or points[-1][0] > observed))
    failed = bool(entry.get('error'))
    return dict(id=sid, url=(f'https://finance.yahoo.com/quote/{quote(sid, safe="")}/history/'
                            if sid in YAHOO else f'https://fred.stlouisfed.org/series/{sid}'),
                observed=observed, retrieved=entry.get('retrieved'),
                origin='Yahoo Finance' if sid in YAHOO else 'FRED', frequency=frequency,
                age=age, maxAge=MAX_AGE[frequency],
                status='missing' if not observed else 'stale' if failed or stale else 'ok',
                fallback=bool(observed and (failed or stale)))


def build(raw, previous, generated_at, today=None):
    today = today or datetime.now(timezone.utc).date()
    computed = calculate(raw)
    old_metrics = {m['id']: m for m in previous.get('metrics', [])}
    metrics = []
    for mid, section, title, unit, deps, lag, period in SPECS:
        old = old_metrics.get(mid, {})
        sources = [source_metadata(sid, raw, today) for sid in deps]
        points = copy.deepcopy(computed[mid])
        usable = [p for p in points if finite(p[1])]
        # Preserve a whole previously calculated metric if a dependency download fails.
        # This avoids combining revised fresh inputs with an unavailable source's cache.
        dependency_failed = any(raw.get(sid, {}).get('error') for sid in deps)
        use_old = finite(old.get('value')) and (dependency_failed or not usable or
                  (old.get('date') and old['date'] > usable[-1][0]))
        if use_old:
            metric = copy.deepcopy(old)
            # Keep provenance of the values actually displayed, not today's unused inputs.
            held_sources = copy.deepcopy(old.get('sources', sources))
            for source in held_sources:
                source['age'] = ((today-date.fromisoformat(source['observed'])).days
                                 if source.get('observed') else None)
                source.update(status='stale', fallback=True)
            metric.update(status='stale', sources=held_sources)
            metrics.append(metric)
            continue
        if usable:
            points = [p for p in points if p[0] >= usable[0][0]]
            last = usable[-1]
            index = next(i for i, p in enumerate(points) if p[0] == last[0])
            older = points[index-lag][1] if index >= lag else None
            delta = last[1]-older if finite(older) else None
        else:
            last, delta = [None, None], None
        missing_tail = bool(usable and points[-1][0] > last[0])
        # Joint series may lag even when each individual dependency is current.
        joint_age = (today-date.fromisoformat(last[0])).days if last[0] else None
        joint_limit = max(MAX_AGE[FREQUENCIES[sid]] for sid in deps)
        stale = missing_tail or (joint_age is not None and joint_age > joint_limit)
        if stale:
            for source in sources:
                if source['status'] == 'ok':
                    source.update(status='stale', fallback=True)
        secondary = ({'label': '3개월 연율', 'points': computed['pce_secondary']}
                     if mid == 'pce' else None)
        metrics.append(dict(id=mid, section=section, title=title, unit=unit,
                            points=points, date=last[0], value=last[1], delta=delta,
                            period=period, note=old.get('note', ''), formula=FORMULAS[mid],
                            status='missing' if not usable else 'stale' if stale or
                            any(s['status'] != 'ok' for s in sources) else 'ok',
                            sources=sources, secondary=secondary))
    return dict(generatedAt=generated_at, metrics=metrics,
                method='차트는 관측 기준일과 현재 제공되는 수정자료를 사용합니다. 당시 공개정보를 복원한 백테스트가 아닙니다. 다운로드 시각은 발표 시각과 다릅니다. 결측값은 보간하지 않으며 마지막 유효값의 실제 관측일을 유지합니다.')


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n', encoding='utf-8')
    os.replace(tmp, path)


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def refresh(output=ROOT/'data.json', cache=ROOT/'cache'/'observations.json', offline=False):
    previous = read_json(output, {})
    raw = read_json(cache, {})
    successes = 0
    if not offline:
        # FRED requests are independent; Yahoo runs sequentially to avoid shared-state issues.
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(fetch_retry, [s for s in FREQUENCIES if s not in YAHOO]))
        results.extend(fetch_retry(s) for s in sorted(YAHOO))
        for sid, entry, error in results:
            if entry:
                existing = raw.get(sid, {}).get('points', [])
                # Preserve older history when providers limit their downloadable window.
                # New observations (including nulls/revisions) are authoritative within it.
                first, last = entry['points'][0][0], entry['points'][-1][0]
                old_valid = [d for d, v in existing if finite(v)]
                new_valid = [d for d, v in entry['points'] if finite(v)]
                if old_valid and new_valid[-1] < old_valid[-1]:
                    entry['error'] = 'Provider response ends before retained observations'
                merged = {d: v for d, v in existing if d < first or d > last}
                merged.update(dict(entry['points']))
                entry['points'] = [[d, v] for d, v in sorted(merged.items())]
                raw[sid] = entry
                successes += 1
                logging.info('%s: downloaded', sid)
            else:
                raw.setdefault(sid, {'points': [], 'retrieved': None})['error'] = error
                logging.warning('%s: download failed; retaining previous observations: %s', sid, error)
        atomic_json(cache, raw)
    generated_at = max((raw[s]['retrieved'] for s in FREQUENCIES
                        if raw.get(s, {}).get('retrieved') and not raw[s].get('error')), default=None)
    if not successes:
        generated_at = previous.get('generatedAt', generated_at)
    result = build(raw, previous, generated_at)
    atomic_json(output, result)
    logging.info('Wrote %s; %s/%s sources downloaded; %s metrics usable', output, successes,
                 len(FREQUENCIES), sum(m['value'] is not None for m in result['metrics']))
    return successes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'data.json')
    parser.add_argument('--cache', type=Path, default=ROOT/'cache'/'observations.json')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    count = refresh(args.output, args.cache, args.offline)
    if not args.offline and count == 0:
        logging.error('All downloads failed. Last successful generatedAt retained; fallback status saved.')
        raise SystemExit(2)
