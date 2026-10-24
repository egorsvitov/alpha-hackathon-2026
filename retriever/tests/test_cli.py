import json
import sqlite3

from retriever.__main__ import main
from retriever.example import run_example


def test_cli_native_import_validate_inspect_and_offline_cache(tmp_path, capsys):
    fixture = tmp_path/'fixture'
    report = run_example(fixture)
    assert report['new_calls'] == 0 and report['returned'] == 2
    store = tmp_path/'fresh.sqlite3'
    inputs = ['--articles', str(fixture/'articles.json'), '--vectors', str(fixture/'manifest.json'),
              '--clusters', str(fixture/'clusters.json')]
    assert main(['validate', *inputs]) == 0
    assert json.loads(capsys.readouterr().out)['valid']
    assert main(['import', *inputs, '--store', str(store)]) == 0
    capsys.readouterr()
    assert main(['inspect', '--store', str(store)]) == 0
    assert json.loads(capsys.readouterr().out)['active_items'] == 3
    assert main(['validate', '--store', str(store)]) == 0
    capsys.readouterr()
    output = tmp_path/'output'
    assert main(['retrieve', '--store', str(store), '--request', str(fixture/'request.json'),
        '--interests', str(fixture/'interests.json'), '--output', str(output),
        '--merge-state', str(fixture/'merge.sqlite3'), '--encoder-factory', 'retriever.example:encoder_factory']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'ready' and report['new_calls'] == 0
    batch = json.loads((output/'batch.json').read_text())
    trace = json.loads((output/'trace.json').read_text())
    assert len(batch['candidates']) == 2
    assert any(c['item']['kind'] == 'event' for c in batch['candidates'])
    assert trace['calls_new'] == 0


def test_cli_live_requires_explicit_existing_initialized_shared_quota(tmp_path, capsys):
    fixture = tmp_path/'fixture'
    run_example(fixture)
    argv = ['retrieve', '--store', str(fixture/'index.sqlite3'), '--request', str(fixture/'request.json'),
            '--interests', str(fixture/'interests.json'), '--output', str(tmp_path/'out')]
    assert main([*argv, '--max-calls', '1']) == 2
    assert 'live_requires' in capsys.readouterr().err
    request_path = fixture/'request.json'
    request = json.loads(request_path.read_text())
    request['max_new_llm_calls'] = 1
    request_path.write_text(json.dumps(request))
    assert main(argv) == 2
    assert 'explicit_max_calls' in capsys.readouterr().err
    profile = tmp_path/'profile.json'
    profile.write_text('{}')
    quota = tmp_path/'quota.sqlite3'
    full = [*argv, '--max-calls', '1', '--merge-state', str(fixture/'merge.sqlite3'),
            '--profile', str(profile), '--quota-db', str(quota), '--quota-limit', '5']
    assert main(full) == 2
    assert 'quota_db_must_be_existing_file' in capsys.readouterr().err
    assert not quota.exists()
    sqlite3.connect(quota).close()
    assert main(full) == 2
    capsys.readouterr()
    with sqlite3.connect(quota) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    assert not (tmp_path/'out').exists()


def test_cli_errors_are_concise_and_missing_store_is_not_created(tmp_path, capsys):
    missing = tmp_path/'missing.sqlite3'
    assert main(['inspect', '--store', str(missing)]) == 2
    assert not missing.exists()
    assert main(['validate']) == 2
    assert main(['import', '--store', str(missing)]) == 2
    assert not missing.exists()
    assert len(capsys.readouterr().err) < 500
