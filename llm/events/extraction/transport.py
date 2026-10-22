"""Parse one bare JSON object or a single complete Markdown JSON envelope."""
import re
from llm.events.extraction.protocol import load_extraction_json


def decode_stage_json(content):
    """Explicit transport envelope, never repair JSON or alter semantic fields."""
    if isinstance(content,str):
        match=re.fullmatch(r'\s*```json[ \t]*\r?\n(.*?)\r?\n```\s*',content,re.S)
        if match:
            return load_extraction_json(match.group(1)), 'markdown_json'
    return load_extraction_json(content), 'json'
