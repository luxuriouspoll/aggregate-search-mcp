"""Offline synthetic response replay. Never reads user keys or makes HTTP requests."""
import argparse
import asyncio
import inspect
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def replay(cases):
    from aggregate_search import __version__, config, server
    from aggregate_search.providers.base import FetchResult, SearchResult, merge_results, fold_near_duplicates, truncate, normalize_url
    rows = []
    for case in cases:
        started = time.perf_counter()
        groups = {}
        for row in case['results']:
            groups.setdefault(row['source'], []).append(SearchResult(**row))
        merged, _ = fold_near_duplicates(merge_results(groups))
        ranks = [i + 1 for i, item in enumerate(merged) if normalize_url(item['url']) in {normalize_url(u) for u in case['acceptable_sources']}]
        async def fetch(name, url, anonymous=False):
            return FetchResult(name, url, content=truncate(case['body']), auth='anon')
        server._fetch_cache_clear()
        kwargs = {}
        if 'focus' in inspect.signature(server.web_fetch).parameters:
            kwargs = {'focus': case.get('focus', case['query']), 'start_index': case.get('start_index', 0)}
        with config.use_settings(**case.get('settings', {})), patch.object(server, 'run_fetch', side_effect=fetch):
            rendered = server._format_search(case['query'], merged, 'Offline fixture', [], [], {}, {})
            content = await server.web_fetch(case['acceptable_sources'][0], provider='parallel', **kwargs)
        fetch_facts = case.get('fetch_facts', case['facts'])
        search_facts = case.get('search_facts', case['facts'])
        rows.append(dict(id=case['id'], kind=case['kind'], top5_hit=bool(ranks and min(ranks) <= 5), first_rank=min(ranks, default=0),
                         fact_retention=sum(f in content for f in fetch_facts) / len(fetch_facts),
                         search_fact_retention=sum(f in rendered for f in search_facts) / len(search_facts), search_chars=len(rendered), fetch_chars=len(content), keyed_requests=case['keyed_requests'],
                         replay_ms=round((time.perf_counter()-started)*1000, 2), recorded_upstream_ms=case['recorded_elapsed_ms']))
    return {'fixture_type': 'synthetic, not live quality evidence', 'source_version': __version__,
            'fixture_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'cases': rows,
            'summary': {key: round(sum(row[key] for row in rows)/len(rows), 3)
                        for key in ('top5_hit', 'first_rank', 'fact_retention', 'search_fact_retention', 'search_chars', 'fetch_chars', 'keyed_requests', 'replay_ms')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare', type=Path)
    parser.add_argument('--source-root', type=Path, help='Replay the same fixtures against a baseline checkout')
    parser.add_argument('--live', action='store_true', help='Reserved opt-in for a separately budgeted live runner')
    args = parser.parse_args()
    if args.source_root:
        sys.path.insert(0, str(args.source_root.resolve()))
    if args.live:
        parser.error('Live evaluation requires a separately configured response recorder; offline replay is available now.')
    with tempfile.TemporaryDirectory() as home:
        # Isolate all account/configuration inputs before importing the application.
        for key in list(os.environ):
            if key.upper() not in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE', 'PYTHONUTF8', 'PYTHONIOENCODING'}:
                os.environ.pop(key, None)
        os.environ['AGGREGATE_SEARCH_HOME'] = home
        os.environ['SEARCH_NO_ACCOUNT_KEYS'] = 'true'
        with patch('httpx.AsyncClient.send', side_effect=AssertionError('offline evaluation attempted HTTP')):
            report = asyncio.run(replay(json.loads((ROOT/'evals/cases.json').read_text(encoding='utf-8'))))
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding='utf-8'))
        if (baseline.get('fixture_sha256') != report['fixture_sha256']
                or [row['id'] for row in baseline['cases']] != [row['id'] for row in report['cases']]):
            parser.error('Comparison requires reports replayed from the same fixture snapshot.')
        before = baseline['summary']
        report['delta'] = {k: round(v-before[k], 3) for k,v in report['summary'].items()}
        report['regressions'] = [after['id'] for old, after in zip(baseline['cases'], report['cases'])
                                 if any(after[key] < old[key] for key in ('top5_hit', 'fact_retention', 'search_fact_retention'))]
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'cases'}, ensure_ascii=False, indent=2))
    return int(bool(report.get('regressions')))


if __name__ == '__main__':
    sys.exit(main())
