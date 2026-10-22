"""Collect public RSS items and extracted article text; no LLM calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import httpx
import trafilatura

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from ranking.b6 import require_valid_bundle

SOURCES = [('interfax', 'https://www.interfax.ru/rss.asp'),
           ('lenta', 'https://lenta.ru/rss/news'),
           ('kommersant', 'https://www.kommersant.ru/RSS/news.xml')]
UA = 'AlphaHackEmbeddingExperiment/0.1'


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main():
    out = HERE / 'artifacts'
    cache = out / 'http'
    cache.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(follow_redirects=True, timeout=20, headers={'User-Agent': UA})
    candidates, diagnostics, robots, gates = [], [], {}, {}
    lists = []
    for name, url in SOURCES:
        r = client.get(url)
        r.raise_for_status()
        (cache / (name + '.rss')).write_bytes(r.content)
        entries = feedparser.parse(r.content).entries
        selected = []
        for e in entries:
            if not e.get('link') or not e.get('title'):
                continue
            date = None
            if e.get('published'):
                try:
                    dt = parsedate_to_datetime(e.published)
                    if dt.tzinfo is not None:
                        date = dt.astimezone(timezone.utc).isoformat(timespec='seconds')
                except (ValueError, TypeError):
                    pass
            selected.append({'publisher': name, 'url': e.link.split('#')[0], 'title': e.title,
                             'published_at': date, 'rss_summary_html': e.get('summary', '')})
        lists.append(selected)
        print(name, 'RSS entries:', len(selected), flush=True)
    # Interleave sources rather than taking the first 100 from one publisher.
    seen = set()
    for i in range(max(map(len, lists))):
        for items in lists:
            if i < len(items) and items[i]['url'] not in seen:
                candidates.append(items[i]); seen.add(items[i]['url'])
    for host in {urlparse(c['url']).netloc for c in candidates}:
        gates[host] = threading.Semaphore(2)
        robot_url = 'https://' + host + '/robots.txt'
        try:
            response = client.get(robot_url)
            if response.status_code == 200:
                parser = RobotFileParser(robot_url)
                parser.parse(response.text.splitlines())
                robots[host] = parser
                (cache / (host + '-robots.txt')).write_text(response.text)
        except httpx.HTTPError:
            pass

    def fetch(item):
        def rss_fallback(reason):
            class Text(HTMLParser):
                def __init__(self): super().__init__(); self.parts=[]
                def handle_data(self, data): self.parts.append(data)
            parser=Text(); parser.feed(item.get('rss_summary_html', ''))
            body=' '.join(' '.join(parser.parts).split())
            if len(body) < 100:
                return None, {'url': item['url'], 'reason': reason + ':rss_text_too_short'}
            data={k:v for k,v in item.items() if k != 'rss_summary_html'}
            return {**data, 'article_id': 'experiment:' + digest(item['url'])[:24],
                    'text': body, 'text_sha256': digest(body), 'retrieved_at': stamp(),
                    'extraction': 'rss_summary', 'fallback_reason': reason, 'language': 'ru'}, None
        host = urlparse(item['url']).netloc
        if host in robots and not robots[host].can_fetch(UA, item['url']):
            return rss_fallback('robots_disallowed')
        try:
            with gates[host]:
                response = client.get(item['url'])
                response.raise_for_status()
                html = response.text
                time.sleep(0.2)
            body = trafilatura.extract(html, include_comments=False, include_tables=False,
                                       favor_precision=True, url=item['url']) or ''
            if len(body) < 200:
                return rss_fallback('insufficient_extracted_text')
            item = {**{k:v for k,v in item.items() if k != 'rss_summary_html'}, 'article_id': 'experiment:' + digest(item['url'])[:24],
                    'text': body, 'text_sha256': digest(body), 'retrieved_at': stamp(),
                    'extraction': 'trafilatura_public_html', 'language': 'ru'}
            return item, None
        except Exception as exc:
            return rss_fallback(type(exc).__name__)

    articles, texts = [], set()
    started = time.perf_counter()
    # Small waves limit unnecessary fetching after the target is reached.
    with ThreadPoolExecutor(max_workers=6) as pool:
        for start in range(0, len(candidates), 12):
            for item, error in pool.map(fetch, candidates[start:start+12]):
                if error:
                    diagnostics.append(error)
                elif item['text_sha256'] not in texts and len(articles) < 100:
                    texts.add(item['text_sha256']); articles.append(item)
            print('Collected:', len(articles), 'errors:', len(diagnostics), flush=True)
            if len(articles) >= 100:
                break
    client.close()
    if len(articles) != 100:
        (out / 'partial-collection.json').write_text(json.dumps({'articles':articles,'errors':diagnostics},ensure_ascii=False,indent=2))
        raise RuntimeError(f'Collected {len(articles)} usable articles, expected 100')

    bundles = []
    for a in articles:
        source_id = a['article_id'] + ':v1'
        # Quote-only envelope; unknown semantic fields must not be fabricated.
        lead = next((line.strip() for line in a['text'].splitlines() if len(line.strip()) >= 120), a['text'])[:1200]
        start = a['text'].index(lead)
        claim_id, evidence_id = a['article_id'] + ':claim', a['article_id'] + ':evidence'
        b = {'schema_version': '0.2',
             'event': {'event_id': a['article_id'], 'event_version': 1, 'title': a['title'],
                       'event_type': 'other', 'first_published_at': a['published_at'],
                       'updated_at': a['retrieved_at'], 'availability': 'active', 'quality': 'partial'},
             'ranking_features': {'topics': ['other'], 'industries': [], 'geographies': [],
                                  'objects': [], 'metrics': [], 'factors': []},
             'claims': [{'claim_id': claim_id, 'text': lead, 'modality': 'unknown',
                         'polarity': 'unknown', 'stage': None, 'attribution': None,
                         'participants': [], 'action': None, 'object': None, 'geographies': [],
                         'time': dict.fromkeys(['occurred_at','effective_at','observation_period','forecast_horizon']),
                         'measurements': [], 'scenario': None, 'evidence_ids': [evidence_id],
                         'field_evidence': {k: [evidence_id] for k in ['/text','/modality','/polarity']}}],
             'sources': [{'source_id': source_id, 'article_id': a['article_id'], 'article_version': 1,
                          'title': a['title'], 'publisher': a['publisher'], 'url': a['url'],
                          'published_at': a['published_at'], 'content_kind': 'original',
                          'is_full_original_text': False, 'is_synthetic': False}],
             'evidence': [{'evidence_id': evidence_id, 'source_id': source_id, 'field': 'text',
                           'start': start, 'end': start + len(lead), 'quote': lead}],
             'conflicts': [], 'relations': [],
             'limitations': ['Embedding benchmark: quote-only deterministic packaging, NOT LLM fact extraction.',
                             'Article identity is used as event identity; cross-article event clustering not performed.',
                             'Semantic fields unknown; extracted HTML is not certified complete original.']}
        require_valid_bundle(b)
        assert a['text'][start:start+len(lead)] == b['evidence'][0]['quote']
        bundles.append(b)
    for filename, data in [('articles.jsonl', articles), ('events.v0.2.jsonl', bundles)]:
        (out / filename).write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in data))
    (out / 'events.v0.2.json').write_text(json.dumps(bundles, ensure_ascii=False, indent=2)+'\n')
    manifest = {'collected_at': stamp(), 'count': len(articles), 'sources': SOURCES,
                'collection_seconds': time.perf_counter()-started, 'errors': diagnostics,
                'sources_distribution': {name: sum(a['publisher']==name for a in articles) for name,_ in SOURCES},
                'extraction_distribution': {kind: sum(a['extraction']==kind for a in articles)
                                            for kind in ['rss_summary','trafilatura_public_html']},
                'files': {name: hashlib.sha256((out/name).read_bytes()).hexdigest()
                          for name in ['articles.jsonl','events.v0.2.jsonl','events.v0.2.json']}}
    (out / 'collection.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:v for k,v in manifest.items() if k != 'errors'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
