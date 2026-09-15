#!/usr/bin/env python3
"""Replay the recorded extraction call through the real tool runtime, without an LLM.

This complements, and never substitutes for, the real browser smoke. Input must
be an isolated fixture prepared by prepare_dataset_reuse_fixture.py.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from epi_agent.db_rag.tools import build_db_rag_tool_registry
from epi_agent.protocol import ToolContext
from epi_agent.runtime import EpiAgentRuntimeConfig, _execute_tools
from study_package.registry import discover_studies
from utils.model_runtime_profiles import model_runtime_profile
from utils.user_storage import UserStorageLayout
from utils.dataset_artifacts import load_dataset_artifact


def replay(fixture: Path) -> dict:
    manifest = json.loads((fixture / 'fixture.json').read_text())
    runtime = Path(manifest['runtime_root']).resolve()
    assert fixture.resolve() in runtime.parents
    with sqlite3.connect(f'file:{fixture.resolve()}/checkpoint-fixture.db?mode=ro',uri=True) as c:
        typ, data = c.execute('select type,checkpoint from checkpoints where thread_id=? and checkpoint_id=?',(manifest['thread_id'],manifest['checkpoint_id'])).fetchone()
    values = JsonPlusSerializer().loads_typed((typ,data))['channel_values']
    old = deepcopy(values['artifacts']['datasets'][manifest['dataset_id']])
    hashes = {k:hashlib.sha256(Path(old[k]).read_bytes()).hexdigest() for k in ('path','schema_path','metadata_path')}
    assert all(runtime in Path(old[k]).resolve().parents for k in hashes)
    state = {**values, 'terminal_error': None, 'terminal_control': None, 'completion_blocked': False, 'failure_signatures': [], 'current_turn_artifact_refs': [], 'current_turn_output_artifact_refs': []}
    call = {'name':'dbrag-validate_and_extract','args':{'plan_id':manifest['plan_id'],'plan_version':manifest['plan_version'],'sql':None,'predecessor_dataset_id':None,'predecessor_dataset_version':None},'id':'historical-extraction-replay','type':'tool_call'}
    state['messages'] = [*values['messages'],HumanMessage(content=manifest['query']),AIMessage(content='',tool_calls=[call])]
    studies = discover_studies(Path(manifest['study_root'])/'studies')
    scope = UserStorageLayout(runtime).thread('local-user',manifest['thread_id'])
    config = EpiAgentRuntimeConfig(agent_name='historical_replay',system_prompt='',registry=build_db_rag_tool_registry(),studies=studies,context_factory=lambda _state,_config,store:ToolContext(studies=studies,artifact_store=store,thread_id=manifest['thread_id'],thread_storage=scope,policy=None),model_profile=model_runtime_profile('gpt-5.6-terra'))
    result = _execute_tools(state,{'configurable':{'thread_id':manifest['thread_id']}},agent_config=config)
    if result.get('terminal_error'):
        raise AssertionError(result['terminal_error'])
    errors = [json.loads(m.content)['error'] for m in result['messages'] if isinstance(m.content,str) and m.content.startswith('{') and 'error' in json.loads(m.content)]
    assert not errors, errors
    candidates = [(k,v) for k,v in result['artifacts']['datasets'].items() if k != manifest['dataset_id']]
    assert len(candidates)==1, [k for k,_ in candidates]
    new_id,new=candidates[0]
    assert new['status']=='pending_review'
    assert new['provenance']['plan_id']==manifest['plan_id']
    assert result['artifacts']['datasets'][manifest['dataset_id']]==old
    assert hashes=={k:hashlib.sha256(Path(old[k]).read_bytes()).hexdigest() for k in hashes}
    frame,schema=load_dataset_artifact(new,runtime_root=scope)
    second_state={**state,**result,'messages':[*state['messages'],*result['messages'],AIMessage(content='',tool_calls=[{**call,'id':'historical-replay-again'}])]}
    second=_execute_tools(second_state,{'configurable':{'thread_id':manifest['thread_id']}},agent_config=config)
    assert not second.get('terminal_error'),second.get('terminal_error')
    assert set(second['artifacts']['datasets'])==set(result['artifacts']['datasets'])
    receipt={'mode':'recorded tool call through production runtime; no LLM invoked','query':manifest['query'],'old_dataset_id':manifest['dataset_id'],'fresh_dataset_id':new_id,'status':new['status'],'rows':len(frame),'columns':list(frame.columns),'plan_id':manifest['plan_id'],'plan_version':manifest['plan_version'],'old_record_and_files_unchanged':True,'replay_reuses_fresh_id':True,'terminal_error':None}
    (fixture/'candidate-replay.json').write_text(json.dumps(receipt,indent=2))
    print(json.dumps(receipt,indent=2))
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--fixture',type=Path,required=True)
    replay(p.parse_args().fixture)
