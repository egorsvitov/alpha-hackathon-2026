"""sentences-rows.v1: exact code-owned boundaries, never model-counted offsets.

Conservative heuristic boundaries are selection units, not linguistic annotations.
Abbreviations and decimals are deliberately kept inside a unit. Table lines stay whole.
"""
import re
from llm.events.extraction.fragments import source_fragments
from llm.events.storage.store import StorageError

VERSION = 'span-ids.v1'
SEGMENTATION_VERSION = 'sentences-rows.v1'


def fragments_with_spans(record):
    result = []
    for fragment in source_fragments(record):
        text = fragment['text']
        # Split prose only after terminal punctuation followed by a capital letter.
        # Newlines are boundaries as well, preserving table rows and their headers.
        boundaries = [0]
        for match in re.finditer(r'\n+|(?<=[!?])\s+(?=[А-ЯЁA-Z«"“])|(?<=\.)\s+(?=[А-ЯЁA-Z«"“])', text):
            prefix = text[:match.start()]
            if '\n' not in match.group() and re.search(r'\b(?:руб|коп|млн|млрд|трлн|тыс|г|гг|им|т|д)\.$', prefix, re.I):
                continue
            boundaries.append(match.end())
        boundaries.append(len(text))
        spans = []
        for start, end in zip(boundaries, boundaries[1:]):
            while start < end and text[start].isspace(): start += 1
            while end > start and text[end - 1].isspace(): end -= 1
            if start < end:
                spans.append({'id':f"{fragment['id']}.S{len(spans)+1}",
                              'start':fragment['start']+start,'end':fragment['start']+end,
                              'text':text[start:end]})
        result.append({**fragment, 'spans':spans})
    return result


def locate_span(ref, record):
    fragments = {f['id']:f for f in fragments_with_spans(record)}
    if ref.fragment_id not in fragments:
        raise StorageError('unknown_fragment', ref.fragment_id)
    fragment = fragments[ref.fragment_id]
    units = {s['id']:i for i,s in enumerate(fragment['spans'])}
    if ref.start_id not in units or ref.end_id not in units:
        raise StorageError('unknown_span', 'Span IDs must belong to the specified fragment')
    lo, hi = units[ref.start_id], units[ref.end_id]
    if lo > hi:
        raise StorageError('invalid_span_order', 'Start must precede end')
    start, end = fragment['spans'][lo]['start'], fragment['spans'][hi]['end']
    return {'field':fragment['field'], 'start':start, 'end':end,
            'quote':getattr(record.article,fragment['field'])[start:end], 'fragment_id':ref.fragment_id}


def wire_schema(schema):
    """New requests accept ID references only; legacy parser still accepts quotes."""
    if isinstance(schema, list): return [wire_schema(x) for x in schema]
    if not isinstance(schema, dict): return schema
    choices = schema.get('anyOf')
    if choices and {x.get('$ref') for x in choices} == {'#/$defs/QuoteRef','#/$defs/SpanRef'}:
        return {'$ref':'#/$defs/SpanRef'}
    return {k:wire_schema(v) for k,v in schema.items() if k != 'QuoteRef'}

INSTRUCTION = '''
Формат оснований span-ids.v1. Фрагменты содержат spans с готовыми ID предложений/строк.
НЕ повторяй цитаты и НЕ считай символы. Вместо quote укажи fragment_id, start_id и end_id.
Для одного предложения start_id=end_id; непрерывный диапазон может охватывать несколько
соседних spans ОДНОГО фрагмента. Например:
{"fragment_id":"P1","start_id":"P1.S1","end_id":"P1.S2"}.
Для раздельных оснований используй несколько ссылок. Заголовки таблиц и единицы включай
отдельными ссылками на их строки. Нельзя изобретать ID. Значения полей по-прежнему заполняй
исходными выражениями; только доказательства выбирай по ID. Наличие ссылки не доказывает
смысловую роль поля. Эти правила заменяют старые указания повторять quote/trigger-цитату.
'''
