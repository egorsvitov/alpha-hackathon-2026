"""Generate a tiny synthetic fixture and shared schemas without model/API imports."""
import json
from pathlib import Path
from . import contracts as c

MODELS = (c.NewsDocumentRef, c.CompanyQuery, c.RawNewsCandidateBatch, c.ExperimentManifest, c.NewsAnalysisInput)


def fixture():
    text = "Синтетический пример: опубликованы новые условия финансирования оборудования."
    doc = c.RawNewsDocument(reference=c.NewsDocumentRef(document_id="synthetic-news-1", article_id="synthetic:1",
        content_hash=c.text_hash(text), snapshot_id="synthetic-snapshot-v1", source="synthetic-fixture",
        published_at="2026-10-20T12:00:00+03:00", publication_precision="timestamp"),
        title="Учебный пример", title_origin="source", text=text)
    query = c.company_query(company_id="synthetic-company-1", profile_version="synthetic-profile-v1",
        text="Финансирование покупки оборудования", family="mechanisms", view="finance",
        anchor_feature_ids=["synthetic-company-1:/products/loan"], template_version="synthetic-mechanisms.v1", status="conditional")
    hit = c.QueryHit(document_id=doc.reference.document_id, query_id=query.query_id, family="mechanisms", channel="lexical", rank=1, raw_score=1.25)
    batch = c.RawNewsCandidateBatch(company_id=query.company_id, profile_version=query.profile_version,
        as_of="2026-10-22T23:59:59+03:00", snapshot_id=doc.reference.snapshot_id, queries=[query], documents=[doc],
        candidates=[c.CandidateTrace(document_id=doc.reference.document_id, hits=[hit], fusion_score=1/61,
            fusion_rank=1, selected=True)], selected_context=[c.ContextSelection(document_id=doc.reference.document_id,
            role="retrieved", spans=[c.SourceSpan(document_id=doc.reference.document_id, content_hash=doc.reference.content_hash, start=0, end=len(text))])],
        budget={"max_documents": 20, "counter": "utf8_bytes_div4_estimate.v1", "limit": 8192})
    return c.NewsAnalysisInput(batch=batch, company_facts=[c.CompanyFact(feature_id=query.anchor_feature_ids[0], value=True, status="observed")], analytical_profile="synthetic-offline.v1")


def write():
    root = Path(__file__).parent
    for model in MODELS:
        (root / "schemas" / (model.__name__ + ".json")).write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
    (root / "fixtures/shared-input.v1.json").write_text(json.dumps(fixture().model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    write()
