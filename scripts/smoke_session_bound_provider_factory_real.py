#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.auth import AuthenticatedUser, LOCAL_SESSION_ID, RequestIdentity
from utils.env_loader import load_app_environment


def main() -> int:
    load_app_environment(PROJECT_ROOT)
    from api.app import build_application

    provider_key = str(os.environ.get("OPENAI_API_KEY", "") or "").strip()
    if not provider_key:
        raise RuntimeError("OPENAI_API_KEY is required for this real smoke")
    original_environment_key = os.environ.get("OPENAI_API_KEY")

    with tempfile.TemporaryDirectory(prefix="epi-session-provider-smoke-") as root:
        temporary_root = Path(root)
        environ = dict(os.environ)
        environ.update(
            {
                "REPORT_AGENT_AUTH_MODE": "local",
                "REPORT_AGENT_RUNTIME_ROOT": str(temporary_root / "runtime"),
                "REPORT_AGENT_STUDY_ROOT": str(temporary_root / "study-data"),
                "REPORT_AGENT_STATIC_DIR": "",
            }
        )
        application = build_application(environ=environ)
        runtime = application.state.report_agent_runtime
        identity = RequestIdentity(
            user=AuthenticatedUser(owner_user_id="local-user"),
            session_id=LOCAL_SESSION_ID,
        )
        assert application.state.provider_credential_store.get(identity) == provider_key

        thread_id = runtime.create_thread(identity)
        cache_key = (identity.owner_user_id, thread_id)
        assert runtime._threads[cache_key].app is None
        runtime.state(identity, thread_id, provider_api_key=provider_key)
        assert runtime._threads[cache_key].app is not None
        assert runtime._threads[cache_key].credential_session_id == LOCAL_SESSION_ID
        assert not hasattr(runtime._threads[cache_key], "provider_api_key")

        runtime.release_session(identity.owner_user_id, identity.session_id)
        assert cache_key not in runtime._threads

    assert os.environ.get("OPENAI_API_KEY") == original_environment_key
    print("session-bound provider factory smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
