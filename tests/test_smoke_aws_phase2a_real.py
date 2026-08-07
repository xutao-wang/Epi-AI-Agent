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
