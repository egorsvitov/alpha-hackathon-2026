"""Manual Telegram exports and collector JSONL → immutable, auditable corpus.

Texts are preserved. Rich-text parts are concatenated without rewriting, with
source JSON pointers and Python Unicode coordinates. No networking or LLM calls.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid5, NAMESPACE_URL

from pydantic import ValidationError
from llm.events import ArticleInput, StorageError
from llm.events.contracts.validation import article_sha256
from llm.events.storage.store import canonical_json

VERSION = 'prepared-news.v1'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def flatten_text(value, pointer):
    """Return exact text and a reversible part-to-character mapping."""
    parts = [(pointer, value)] if isinstance(value, str) else []
    if isinstance(value, list):
        for index, item in enumerate(value):
            path = f'{pointer}/{index}'
            if isinstance(item, str):
                parts.append((path, item))
            elif isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append((path+'/text', item['text']))
            else:
                raise ValueError('unsupported_rich_text_part')
    elif not isinstance(value, str):
        raise ValueError('invalid_text')
    text = ''; spans = []
    for path, part in parts:
        start = len(text); text += part
        spans.append({'pointer':path, 'start':start, 'end':len(text)})
    return text, spans


def publication(raw, epoch=None):
    """Never invent midnight or a timezone for date-only/naive timestamps."""
    if epoch is not None:
        try:
            if isinstance(epoch, bool) or not re.fullmatch(r'-?\d+', str(epoch)):
                raise ValueError()
            stamp = datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
            return stamp, {'raw':raw, 'epoch':str(epoch), 'precision':'instant', 'basis':'unix_timestamp'}
        except (ValueError, OverflowError, OSError):
            raise ValueError('invalid_epoch') from None
    if raw is None:
        return None, {'raw':None, 'precision':'unknown'}
    if not isinstance(raw, str):
        raise ValueError('invalid_publication_date')
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            date.fromisoformat(raw)
            return None, {'raw':raw, 'date':raw, 'precision':'day', 'basis':'source_calendar_date'}
        parsed = datetime.fromisoformat(raw)
        if 'T' not in raw:
            raise ValueError()
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, {'raw':raw, 'date':parsed.date().isoformat(), 'precision':'local_datetime', 'basis':'unknown_timezone'}
        return raw, {'raw':raw, 'precision':'instant', 'basis':'source_timestamp'}
    except ValueError:
        raise ValueError('invalid_publication_date') from None


def read_inputs(directory):
    """Yield file manifests and source rows. Original files are never changed."""
    files = []; rows = []
    for path in sorted(Path(directory).iterdir()):
        if path.suffix not in ('.json', '.jsonl') or not path.is_file():
            continue
        raw = path.read_bytes()
        info = {'file':path.name, 'sha256':sha(raw), 'bytes':len(raw)}
        files.append(info)
        if path.suffix == '.jsonl':
            for number, line in enumerate(raw.splitlines(), 1):
                if not line.strip(): continue
                origin = {'file':path.name, 'file_sha256':info['sha256'], 'line':number, 'format':'collector_jsonl'}
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    rows.append((None, origin, 'invalid_json')); continue
                rows.append((row, origin, None))
        else:
            try:
                exported = json.loads(raw)
            except (ValueError, UnicodeError):
                rows.append((None, {'file':path.name}, 'invalid_json')); continue
            if (not isinstance(exported, dict) or exported.get('type') != 'public_channel'
                    or type(exported.get('id')) is not int or not isinstance(exported.get('messages'), list)):
                rows.append((None, {'file':path.name}, 'unsupported_telegram_export')); continue
            info.update(channel_id=exported['id'], channel_name=exported.get('name'))
            for index, row in enumerate(exported['messages']):
                origin = {'file':path.name, 'file_sha256':info['sha256'], 'pointer':f'/messages/{index}',
                          'format':'telegram_desktop', 'channel_id':exported['id'], 'channel_name':exported.get('name')}
                rows.append((row, origin, None))
    if not files:
        raise StorageError('empty_raw_corpus', 'No JSON/JSONL source files found')
    return files, rows


def prepare_row(row, origin):
    if not isinstance(row, dict): raise ValueError('invalid_record')
    telegram = origin['format'] == 'telegram_desktop'
    if telegram and row.get('type') != 'message': raise ValueError('service_message')
    pointer = origin.get('pointer', '')+'/text'
    text, spans = flatten_text(row.get('text', ''), pointer)
    if not text.strip(): raise ValueError('empty_text')
    timestamp, time = publication(row.get('date') if telegram else row.get('published_at'),
                                  row.get('date_unixtime') if telegram else None)
    if telegram:
        if type(row.get('id')) is not int: raise ValueError('invalid_message_id')
        channel = origin['channel_id']
        values = {'article_id':f'telegram:{channel}:{row["id"]}', 'source':origin['channel_name'], 'url':None,
                  'title':f'{origin["channel_name"]} — сообщение {row["id"]}', 'language':'und'}
        origin.update(message_id=row['id'], title_origin='generated_identifier',
                      edited_raw=row.get('edited'), edited_epoch=row.get('edited_unixtime'),
                      reply_to_message_id=row.get('reply_to_message_id'), forwarded_from=row.get('forwarded_from'))
        # Exported text is the original post, not necessarily a full linked article.
        origin['limitations'] = ['Post text only; linked article and media were not fetched.']
        if row.get('text_entities') is not None:
            try:
                other, _ = flatten_text(row['text_entities'], pointer.rsplit('/', 1)[0]+'/text_entities')
            except ValueError:
                other = None
            if other != text: origin['limitations'].append('text_entities differs from text or is unreadable; text is authoritative.')
    else:
        values = {key:row.get(key) for key in ('article_id','source','url','title','language')}
        values['language'] = row.get('language', 'ru')
        origin.update(title_origin='source', limitations=['Collector extraction has not been checked for completeness or boilerplate.'])
    origin.update(publication=time, text_parts=spans)
    return ArticleInput(**values, text=text, published_at=timestamp, content_kind='original',
                        is_full_original_text=False, is_synthetic=False)


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def prepare_corpus(directory, output):
    directory, output = Path(directory), Path(output)
    if directory.resolve() == output.resolve():
        raise StorageError('invalid_output_path', 'Prepared output must differ from raw input')
    files, rows = read_inputs(directory)
    implementation = sha(Path(__file__).read_bytes())
    corpus_id = sha(canonical_json({'version':VERSION, 'files':files, 'implementation_sha256':implementation,
                                    'article_schema':ArticleInput.model_json_schema()}).encode())
    if output.exists():
        manifest = json.loads((output/'manifest.json').read_text())
        if manifest['corpus_id'] != corpus_id:
            raise StorageError('corpus_changed', 'Use a new output directory for changed source files')
        for name, expected in manifest['outputs'].items():
            if sha((output/name).read_bytes()) != expected:
                raise StorageError('corpus_output_corrupt', name)
        return {'status':'cached', 'output':str(output), 'manifest':manifest}
    documents = {}; rejected = []; by_file = defaultdict(Counter)
    for row, origin, error in rows:
        try:
            if error: raise ValueError(error)
            article = prepare_row(row, origin)
            digest = article_sha256(article)
            document_id = str(uuid5(NAMESPACE_URL, 'alpha-corpus:'+digest))
            if document_id not in documents:
                documents[document_id] = {'document_id':document_id, 'article_sha256':digest,
                    'text_sha256':sha(article.text.encode()), 'article':article.model_dump(mode='json'), 'provenance':[]}
            documents[document_id]['provenance'].append(origin)
            by_file[origin['file']]['accepted'] += 1
        except (ValueError, ValidationError) as exc:
            code = 'invalid_article' if isinstance(exc, ValidationError) else str(exc)
            rejected.append({'origin':origin, 'code':code})
            by_file[origin['file']][code] += 1
    docs = sorted(documents.values(), key=lambda x:x['document_id'])
    texts = defaultdict(list); sources = Counter(); dates = []; precision = Counter()
    long = []; versions = defaultdict(list)
    for doc in docs:
        article = doc['article']; sources[article['source']] += 1
        texts[doc['text_sha256']].append(doc['document_id'])
        versions[article['article_id']].append(doc['document_id'])
        if len(article['text']) > 20000: long.append(doc['document_id'])
        for origin in doc['provenance']:
            precision[origin['publication']['precision']] += 1
            if article['published_at']: dates.append(article['published_at'][:10])
            elif origin['publication'].get('date'): dates.append(origin['publication']['date'])
    duplicates = [ids for ids in texts.values() if len(ids)>1]
    ambiguous = {aid:ids for aid,ids in versions.items() if len(ids)>1}
    audit = {'schema_version':'corpus-audit.v1', 'observations':len(rows), 'accepted_observations':sum(v['accepted'] for v in by_file.values()),
        'documents':len(docs), 'excluded_or_invalid':len(rejected), 'sources':dict(sources.most_common()),
        'by_file':{k:dict(v) for k,v in by_file.items()}, 'date_precision':dict(precision),
        'calendar_range':[min(dates), max(dates)] if dates else None,
        'exact_text_groups':duplicates, 'exact_text_duplicate_excess':sum(len(g)-1 for g in duplicates),
        'multiple_versions':ambiguous, 'long_document_ids':long,
        'limitations':['No event labels or quality estimate.', 'Repeated text is not automatically an event duplicate.',
                       'Boilerplate, missing media and incomplete article extraction require review.',
                       'No automatic train/test split: related events must be adjudicated first.']}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.'+output.name+'-', dir=output.parent))
    try:
        (temporary/'documents.jsonl').write_text(''.join(canonical_json(d)+'\n' for d in docs), encoding='utf-8')
        _write(temporary/'audit.json', audit); _write(temporary/'rejected.json', rejected)
        if not ambiguous: _write(temporary/'articles.json', [d['article'] for d in docs])
        outputs = {p.name:sha(p.read_bytes()) for p in temporary.iterdir()}
        manifest = {'schema_version':VERSION, 'corpus_id':corpus_id, 'implementation_sha256':implementation, 'inputs':files, 'outputs':outputs,
                    'b2_batch_available':not ambiguous, 'documents':len(docs)}
        _write(temporary/'manifest.json', manifest)
        temporary.rename(output)
    finally:
        if temporary.exists(): shutil.rmtree(temporary)
    return {'status':'prepared', 'output':str(output), 'manifest':manifest, 'audit':audit}
