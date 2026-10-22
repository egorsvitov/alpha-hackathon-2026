"""The collector's JSONL → ArticleInput array → immutable B2 archive."""
from pathlib import Path
import argparse
import json
from pydantic import ValidationError
from news.core.model import Article


def to_article_input_dict(article: Article) -> dict:
    """Preserve collector fields; a nonempty body does not prove completeness."""
    return {**article.model_dump(mode='json'), 'content_kind':'original',
            'is_full_original_text':False, 'is_synthetic':False}


def load_news_corpus(jsonl_path: str | Path) -> list[dict]:
    """Skip empty/invalid collector rows; B2 validates the remaining articles."""
    path = Path(jsonl_path)
    if not path.exists():
        return []
    articles = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            article = Article.model_validate_json(line)
        except ValidationError:
            continue
        if article.text.strip():
            articles.append(to_article_input_dict(article))
    return articles


def main(argv=None):
    from llm.events.storage.store import EventStore, DEFAULT_DB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('jsonl', type=Path)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    if not args.jsonl.is_file():
        parser.error('JSONL file does not exist')
    articles = load_news_corpus(args.jsonl)
    receipt = EventStore(args.db).import_batch(json.dumps(articles, ensure_ascii=False).encode('utf-8'))
    print(receipt.model_dump_json(indent=2))
    return 0 if receipt.import_status == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
