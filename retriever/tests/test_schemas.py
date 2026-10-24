import json
from pathlib import Path

import jsonschema
import pytest

import retriever
from retriever.example import run_example
from retriever.schemas import PUBLIC_MODELS, SCHEMA_VERSION, generate, main, schema_document

SCHEMAS = Path(__file__).resolve().parents[1]/'schemas'


def test_public_exports_and_checked_in_schema_drift(tmp_path):
    generated = generate(tmp_path)
    assert len(generated) == len(PUBLIC_MODELS)
    assert {p.name for p in SCHEMAS.glob('*.json')} == {p.name for p in generated}
    for model, path in zip(PUBLIC_MODELS, generated):
        assert getattr(retriever, model.__name__) is model
        assert model.__name__ in retriever.__all__
        assert path.read_bytes() == (SCHEMAS/path.name).read_bytes(), f'Regenerate changed schema: {model.__name__}'
        schema = json.loads(path.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema['$id'] == f'urn:retriever:{model.__name__}:{SCHEMA_VERSION}'
        assert schema['additionalProperties'] is False
    assert retriever.RetrieverService and retriever.SQLiteSearchStore and retriever.ClusterMergeAdapter


def test_output_and_input_schemas_validate_synthetic_handoff(tmp_path):
    run_example(tmp_path)
    examples = [('request.json', retriever.RetrievalRequest),
                ('interests.json', retriever.CompanyInterestSet),
                ('batch.json', retriever.RetrievalCandidateBatch)]
    for filename, model in examples:
        value = json.loads((tmp_path/filename).read_text())
        jsonschema.Draft202012Validator(schema_document(model)).validate(value)
    batch = json.loads((tmp_path/'batch.json').read_text())
    validator = jsonschema.Draft202012Validator(schema_document(retriever.RetrievalCandidateBatch))
    invalid = dict(batch, schema_version='retrieval-candidate-batch.v2')
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = dict(batch, unexpected_business_rank=1)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = dict(batch)
    del invalid['sources']
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)
    invalid = dict(batch, candidates=[dict(batch['candidates'][0], score_origin='guessed')])
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(invalid)


def test_schema_cli_writes_only_versioned_contracts(tmp_path, capsys):
    assert main(['--output', str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['schema_version'] == 'v1' and report['schemas'] == len(PUBLIC_MODELS)
    assert len(list(tmp_path.iterdir())) == len(PUBLIC_MODELS)
