import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location("cli",Path(__file__).parents[1]/"scripts/aws_phase2a.py"); cli=importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
class R:
 def __init__(self,out): self.out=out; self.calls=[]
 def run(self,a,*,capture_output=True):
  import subprocess; self.calls.append(list(a)); return subprocess.CompletedProcess(a,0,self.out,"")
def test_identity_target_and_aws_flags():
 r=R('{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/x"}')
 assert cli.require_expected_identity(r)["Account"]==cli.EXPECTED_ACCOUNT
 assert r.calls[0][-4:]==["--profile","xutao-dev","--region","us-east-1"]
def test_wrong_identity_stops_after_one_call():
 r=R('{"Account":"1","Arn":"arn:aws:iam::1:user/x"}')
 import pytest
 with pytest.raises(cli.OperatorError): cli.require_expected_identity(r)
 assert len(r.calls)==1
def test_malformed_or_failed_identity_stops_after_one_call():
 import pytest, subprocess
 for output,code in (("{}",0),("not json",0),("",255)):
  class Bad(R):
   def run(self,a,*,capture_output=True):
    self.calls.append(list(a)); return subprocess.CompletedProcess(a,code,output,"expired credentials")
  r=Bad(output)
  with pytest.raises(cli.OperatorError): cli.require_expected_identity(r)
  assert len(r.calls)==1
def test_subprocess_runner_accepts_capture_output_false(monkeypatch):
 seen={}
 def fake(argv,**kwargs): seen.update(kwargs); import subprocess; return subprocess.CompletedProcess(argv,0,"","")
 monkeypatch.setattr(cli.subprocess,"run",fake)
 cli.SubprocessRunner().run(["true"],capture_output=False)
 assert seen["capture_output"] is False

def _source(): return (Path(__file__).parents[1]/"scripts/aws_phase2a.py").read_text()
def test_bootstrap_plan_is_direct_and_never_executes():
 s=_source(); assert "def plan_bootstrap" in s and 'create-change-set' in s and 'execute-change-set' not in s[s.index("def plan_bootstrap"):s.index("def execute(")]
def test_application_plan_resolves_execution_role_and_polls():
 s=_source(); assert "role=bootstrap_role(r)" in s and "CHANGE_SET_TIMEOUT_SECONDS" in s
def test_execute_change_set_rechecks_exact_arn_stack_and_status():
 s=_source(); assert 'd.get("ChangeSetId",d.get("ChangeSetArn"))!=arn' in s and 'd.get("Status")!="CREATE_COMPLETE"' in s
def test_validate_runs_lint_before_aws_validation():
 s=_source(); assert s.index('"cfn-lint"') < s.index('"validate-template"')
def test_upload_enforces_release_and_study_prefixes():
 s=_source(); assert '("upload-release","releases/")' in s and '("upload-study","studies/")' in s
def test_upload_has_checksum_size_and_no_overwrite_contract():
 s=_source(); assert '"--expected-size"' in s and '"--metadata"' in s and '"refusing overwrite with different checksum"' in s
def test_upload_rejects_non_404_head_errors():
 assert '"404" not in (head.stderr or "")' in _source()
def test_deploy_sends_command_and_polls_terminal_status():
 s=_source(); assert '"send-command"' in s and '"get-command-invocation"' in s and '"TimedOut","Cancelled"' in s
def test_lifecycle_requires_stack_output_and_exact_confirmation():
 s=_source(); assert 'outputs(r).get("ApplicationInstanceId"' in s and 'confirm!=instance' in s
def test_all_aws_commands_use_immutable_target_flags():
 s=_source(); assert 'EXPECTED_PROFILE="xutao-dev"' in s and 'EXPECTED_REGION="us-east-1"' in s and '"--profile",EXPECTED_PROFILE,"--region",EXPECTED_REGION' in s
def test_mutating_helpers_guard_identity_first():
 s=_source()
 for name in ("plan(","plan_bootstrap(","execute(","execute_bootstrap(","upload(","lifecycle(","deploy("):
  start=s.index("def "+name); assert "require_expected_identity(r)" in s[start:start+500]

def test_plan_stack_parser_requires_application_parameters():
 s=_source(); assert 'q.add_argument("--domain-name",required=True)' in s and 'q.add_argument("--hosted-zone-id",required=True)' in s and 'q.add_argument("--certificate-email",required=True)' in s
def test_plan_stack_builds_cloudformation_parameter_values():
 s=_source(); assert '"ParameterKey={k},ParameterValue={v}"' in s and '"DataSnapshotId":a.data_snapshot_id' in s
def test_role_is_placed_only_on_application_change_set_create():
 s=_source(); create=s[s.index('"create-change-set"'):s.index('"describe-change-set"')]; assert '"--role-arn",role' in create
 assert 'describe-change-set","--stack-name",stack,"--change-set-name",name)' in s
def test_ssm_poll_contract_retries_before_terminal_failure():
 s=_source(); assert 'DEPLOY_TIMEOUT_SECONDS' in s and 'time.sleep(POLL_INTERVAL_SECONDS)' in s and '"TimedOut","Cancelled"' in s
def test_change_set_poll_contract_sleeps_and_never_executes():
 s=_source(); section=s[s.index("def plan("):s.index("def plan_bootstrap")]; assert 'time.sleep(POLL_INTERVAL_SECONDS)' in section and 'execute-change-set' not in section

def test_deploy_retries_transient_invocation_then_succeeds(monkeypatch):
 import subprocess
 calls=[]; sleeps=[]
 class SequenceRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(argv)
   if "get-caller-identity" in argv: return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/x"}',"")
   if "send-command" in argv: return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"c"}}',"")
   if sum("get-command-invocation" in x for x in calls)==1: return subprocess.CompletedProcess(argv,255,"","InvocationDoesNotExist")
   if sum("get-command-invocation" in x for x in calls)==2: return subprocess.CompletedProcess(argv,0,'{"Status":"InProgress"}',"")
   return subprocess.CompletedProcess(argv,0,'{"Status":"Success"}',"")
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationBucketName":"b","DeployReleaseDocumentName":"d","ApplicationInstanceId":"i"})
 monkeypatch.setattr(cli.time,"sleep",lambda seconds:sleeps.append(seconds))
 cli.deploy(SequenceRunner(),"releases/a.tgz","a"*64,"b"*40,"epiagent.org","ops@example.org")
 assert len(sleeps)==2
