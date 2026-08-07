import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location("smoke",Path(__file__).parents[1]/"scripts/smoke_aws_phase2a_real.py"); smoke=importlib.util.module_from_spec(spec); spec.loader.exec_module(smoke)
def test_requires_explicit_live_opt_in_and_canonical_url(monkeypatch):
 monkeypatch.setenv("A","a");monkeypatch.setenv("B","b")
 assert smoke.main(["--base-url","https://epiagent.org","--user-one-env","A","--user-two-env","B"])==2
 assert smoke.main(["--allow-live-aws","--base-url","http://epiagent.org","--user-one-env","A","--user-two-env","B"])==2
def test_requires_distinct_named_credential_variables(monkeypatch):
 monkeypatch.setenv("A","secret")
 assert smoke.main(["--allow-live-aws","--base-url","https://epiagent.org","--user-one-env","A","--user-two-env","A"])==2
def test_redacts_credential_values():
 assert "secret" not in smoke.safe_error(Exception("secret exploded"),["secret"])
def test_default_phases_exclude_disruptive_actions():
 assert "stop_start" not in smoke.DEFAULT and "snapshot_restore" not in smoke.DEFAULT
def test_phase_flags_exist():
 args=smoke.parse_args(["--allow-live-aws","--base-url","https://epiagent.org","--user-one-env","A","--user-two-env","B","--stop-start"])
 assert args.stop_start
def test_rejects_other_https_host(monkeypatch):
 monkeypatch.setenv("A","a");monkeypatch.setenv("B","b")
 assert smoke.main(["--allow-live-aws","--base-url","https://other.example","--user-one-env","A","--user-two-env","B"])==2
def test_request_helper_redacts_mocked_transport_error(monkeypatch):
 def fail(*args,**kwargs): raise RuntimeError("token-123 failed")
 monkeypatch.setattr(smoke,"urlopen",fail)
 try: smoke.request_text("https://epiagent.org",secrets=("token-123",))
 except ValueError as error: assert "token-123" not in str(error) and "[REDACTED]" in str(error)
 else: assert False
