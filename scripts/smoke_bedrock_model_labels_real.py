#!/usr/bin/env python3.12
"""Verify live Bedrock labels in real FastAPI and the compiled frontend once.

This checks presentation and stored model identity. It uses real configured
provider discovery and embeddings, without submitting an inference request.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests
from playwright.sync_api import sync_playwright
from utils.bedrock import resolve_bedrock_settings
from utils.env_loader import load_app_environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--study-root', type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifact_dir.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    with (artifacts / 'started').open('x') as handle:
        handle.write('One real attempt; never rerun automatically.\n')
    started = time.monotonic()
    result = {'status': 'running', 'provider_stubs': False, 'inference_requests': 0}
    process = browser = playwright = page = None

    def save(name, value):
        (artifacts / name).write_text(json.dumps(value, indent=2) + '\n')

    def timeout(*_):
        signal.signal(signal.SIGALRM, hard_timeout)
        signal.alarm(max(1, int(started + 299 - time.monotonic())))
        raise TimeoutError('Five-minute feature smoke deadline reached')

    def hard_timeout(*_):
        result.update(status='failed', seconds=time.monotonic() - started,
                      traceback='Absolute smoke deadline reached during execution or cleanup.')
        save('result.json', result)
        if process and process.poll() is None:
            process.kill()
        os._exit(1)

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(280)
    with (artifacts / 'api.log').open('w') as log:
        try:
            load_app_environment(ROOT)
            environment = dict(os.environ)
            resolve_bedrock_settings(environment)
            assert (args.study_root / 'studies/registry.json').is_file()
            environment.update(
                REPORT_AGENT_RUNTIME_ROOT=str(artifacts / 'runtime'),
                REPORT_AGENT_CHECKPOINT_DB_PATH=str(artifacts / 'runtime/checkpoints.db'),
                REPORT_AGENT_STATIC_DIR=str(ROOT / 'frontend/dist'),
                REPORT_AGENT_STUDY_ROOT=str(args.study_root.resolve()),
                DB_RAG_EMBEDDING_PROFILE='bedrock-cohere-embed-v4',
                PYTHONPATH=str(ROOT),
            )
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            process = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'api.app:build_application',
                 '--factory', '--host', '127.0.0.1', '--port', str(port)],
                cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
            )
            while time.monotonic() - started < 280:
                assert process.poll() is None, 'Backend exited; inspect api.log'
                try:
                    if requests.get(base + '/api/health', timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.25)
            else:
                raise TimeoutError('Backend did not become healthy')
            response = requests.get(base + '/api/runtime/options', timeout=10)
            response.raise_for_status()
            options = response.json()
            save('runtime-options.json', options)
            assert options['embedding_startup_status']['available'], options['embedding_startup_status']
            bedrock = [model for model in options['models'] if model['provider'] == 'bedrock']
            assert bedrock, 'Real Bedrock discovery returned no usable models'
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            page.set_default_timeout(20_000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base, wait_until='networkidle')
            selector = page.get_by_label('Model', exact=True)
            selector.wait_for()
            for model in bedrock:
                label = model['label']
                assert label.strip() and ' · ' not in label, model
                assert not any(marker in label for marker in ('(Amazon Bedrock', '(AWS Bedrock', '(AWS Mantle')), model
                assert selector.locator('option').evaluate_all(
                    '(options, id) => options.filter(option => option.value === id).map(option => option.textContent)',
                    model['id'],
                ) == [label]
            chosen = next((model for model in bedrock if model['id'] == 'bedrock:openai.gpt-5.6-terra'), bedrock[0])
            selector.select_option(chosen['id'])
            assert selector.input_value() == chosen['id']
            response = requests.post(base + '/api/threads', json={'model_name': chosen['id']}, timeout=20)
            response.raise_for_status()
            thread_id = response.json()['thread_id']
            response = requests.get(base + f'/api/threads/{thread_id}/state', timeout=10)
            response.raise_for_status()
            state = response.json()
            save('thread-state.json', state)
            assert state['model_name'] == chosen['id'], state['model_name']
            assert state['model_label'] == chosen['label'], state['model_label']
            assert not errors, errors
            (artifacts / 'page.txt').write_text(page.locator('body').inner_text())
            (artifacts / 'page.html').write_text(page.content())
            page.screenshot(path=str(artifacts / 'page.png'), full_page=True)
            result.update(status='passed', models_checked=len(bedrock), selected=chosen['id'], label=chosen['label'], model_identity_preserved=True, browser_errors=errors)
        except Exception:
            result.update(status='failed', traceback=traceback.format_exc())
            if page:
                try:
                    (artifacts / 'page.txt').write_text(page.locator('body').inner_text())
                    (artifacts / 'page.html').write_text(page.content())
                    page.screenshot(path=str(artifacts / 'page.png'), full_page=True)
                except Exception:
                    pass
        finally:
            signal.signal(signal.SIGALRM, hard_timeout)
            signal.alarm(max(1, int(started + 299 - time.monotonic())))
            if browser:
                browser.close()
            if playwright:
                playwright.stop()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            result['seconds'] = time.monotonic() - started
            save('result.json', result)
            signal.alarm(0)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
