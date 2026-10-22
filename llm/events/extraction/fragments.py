"""Exact paragraph boundaries; IDs are local to an immutable article revision."""
import re
from llm.events.contracts.models import ArticleRecord

def source_fragments(record):
    """Exact Unicode intervals; no text truncation or normalization."""
    record = ArticleRecord.model_validate(record)
    fragments = [{'id':'T1','field':'title','start':0,'end':len(record.article.title),'text':record.article.title}]
    for i, match in enumerate(re.finditer(r'\S.*?(?=\n\s*\n|\Z)', record.article.text, re.S), 1):
        text = match.group().rstrip()
        if text:
            fragments.append({'id':f'P{i}','field':'text','start':match.start(),
                              'end':match.start()+len(text),'text':text})
    return fragments
