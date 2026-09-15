#!/usr/bin/env python3
"""Copy the exact pre-failure checkpoint into isolated verification storage."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

THREAD = 'a9a10994715544d4a624c5f5d18a2ccf'
CHECKPOINT = '1f1b04b5-2d40-6d0a-8032-0a8129104880'
QUERY = 'skip any human checkpoint, Create a household-contact dataset with age, sex, household size, TST result, and IGRA result.'


def prepare(source: Path, destination: Path) -> dict:
    source = source.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError('Fixture destination must be new; existing evidence is never overwritten')
    if destination == source or source in destination.parents:
        raise ValueError('Fixture must be outside the source checkout')
    destination.mkdir(parents=True)
    runtime = destination / 'runtime'
    study = destination / 'study_data'
    shutil.copytree(source / 'runtime/users', runtime / 'users')
    shutil.copytree(source / 'study_data', study)
    fixture_db = destination / 'checkpoint-fixture.db'
    original = sqlite3.connect(f'file:{source}/runtime/agent_memory_fastapi.db?mode=ro', uri=True)
    c = sqlite3.connect(fixture_db)
    original.backup(c)
    original.close()
    serializer = JsonPlusSerializer()
    typ, data = c.execute('select type, checkpoint from checkpoints where thread_id=? and checkpoint_id=?', (THREAD, CHECKPOINT)).fetchone()
    cp = serializer.loads_typed((typ, data))
    values = cp['channel_values']
    assert len(values['messages']) == 44
    assert not any(m.type == 'human' and m.content == QUERY for m in values['messages'])
    datasets = values['artifacts']['datasets']
    assert len(datasets) == 1
    old_id, old = next(iter(datasets.items()))
    assert old['status'] == 'cancelled'
    original_hashes = {k: hashlib.sha256(Path(old[k]).read_bytes()).hexdigest() for k in ('path', 'schema_path', 'metadata_path')}
    replacements = [(str(source / 'runtime'), str(runtime)), (str(source / 'study_data'), str(study))]

    def relocate(value):
        if isinstance(value, str):
            for old_root, new_root in replacements:
                value = value.replace(old_root, new_root)
            return value
        if isinstance(value, BaseMessage):
            return value.model_copy(update={k: relocate(v) for k, v in value.__dict__.items()})
        if isinstance(value, dict):
            return {k: relocate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, tuple):
            return tuple(relocate(v) for v in value)
        return value

    cp = relocate(cp)
    values = cp['channel_values']
    # Relocate only fixture JSON paths; source bytes are untouched.
    for path in runtime.rglob('*.json'):
        payload = json.loads(path.read_text())
        path.write_text(json.dumps(relocate(payload), ensure_ascii=False, indent=2))
    manifests = {}
    attempts = values['artifacts']['dataset_persistence_attempts']
    for dataset_id, attempt in attempts.items():
        manifest = {}
        for key, raw_path in attempt['expected_final_paths'].items():
            p = Path(raw_path).resolve()
            assert runtime in p.parents
            content = p.read_bytes()
            manifest[key] = {'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}
        attempt['manifest'] = manifest
        manifests[dataset_id] = manifest
        journal = Path(attempt['expected_final_paths']['path']).parent / '.persistence_attempts' / f'{dataset_id}.json'
        if journal.exists():
            payload = json.loads(journal.read_text())
            payload['manifest'] = manifest
            journal.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    typ, data = serializer.dumps_typed(cp)
    c.execute('delete from checkpoints where thread_id != ? or checkpoint_id != ?', (THREAD, CHECKPOINT))
    c.execute('delete from writes')
    c.execute('update checkpoints set type=?, checkpoint=?, parent_checkpoint_id=NULL where thread_id=? and checkpoint_id=?', (typ, data, THREAD, CHECKPOINT))
    c.execute('delete from conversation_history where thread_id != ?', (THREAD,))
    c.execute('update conversation_history set updated_at=? where thread_id=?', (cp['ts'], THREAD))
    c.execute('delete from agent_activity_items where run_id in (select id from agent_activity_runs where thread_id != ? or created_at > ?)', (THREAD, cp['ts']))
    c.execute('delete from agent_activity_runs where thread_id != ? or created_at > ?', (THREAD, cp['ts']))
    c.commit()
    c.close()
    old = values['artifacts']['datasets'][old_id]
    receipt = {
        'thread_id': THREAD, 'checkpoint_id': CHECKPOINT, 'query': QUERY,
        'dataset_id': old_id, 'plan_id': old['provenance']['plan_id'],
        'plan_version': old['provenance']['plan_version'],
        'original_count': len(values['messages']), 'checkpoint_timestamp': cp['ts'],
        'runtime_root': str(runtime), 'study_root': str(study),
        'checkpoint_fixture': str(fixture_db), 'source_sha256': original_hashes,
        'relocated_manifests': manifests,
        'relocation': 'Absolute storage prefixes and affected manifest hashes only; source database opened read-only',
    }
    (destination / 'fixture.json').write_text(json.dumps(receipt, indent=2))
    # Validate the original failure against real copied artifact files before fixes.
    from types import SimpleNamespace
    from epi_agent.artifacts import StateArtifactStore
    from epi_agent.db_rag.tools import _committed_attempt_result
    try:
        _committed_attempt_result(SimpleNamespace(artifact_store=StateArtifactStore(values['artifacts'])), attempt=attempts[old_id], reused=True)
    except Exception as error:
        failure = {'type': type(error).__name__, 'code': getattr(error, 'code', None), 'message': str(error), 'recoverable': getattr(error, 'recoverable', None)}
        (destination / 'baseline-reproduction.json').write_text(json.dumps(failure, indent=2))
        print(json.dumps(failure))
    else:
        print('Current helper accepted the relocated snapshot; check baseline reproduction against baseline code.')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.source, args.destination)
    print('Prepared isolated fixture:', args.destination)
