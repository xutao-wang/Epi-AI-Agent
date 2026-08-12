import builtins
import importlib.util
import json
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location("cli",Path(__file__).parents[1]/"scripts/aws_phase2a.py"); cli=importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
class R:
 def __init__(self,out): self.out=out; self.calls=[]
 def run(self,a,*,capture_output=True):
  import subprocess; self.calls.append(list(a)); return subprocess.CompletedProcess(a,0,self.out,"")
def test_identity_target_and_aws_flags():
 r=R('{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}')
 assert cli.require_expected_identity(r)["Account"]==cli.EXPECTED_ACCOUNT
 assert r.calls[0][-6:]==["--profile","xutao-dev","--region","us-east-1","--output","json"]
def test_wrong_identity_stops_after_one_call():
 r=R('{"Account":"1","Arn":"arn:aws:iam::1:user/x"}')
 import pytest
 with pytest.raises(cli.OperatorError): cli.require_expected_identity(r)
 assert len(r.calls)==1
def test_same_account_wrong_principal_stops_after_one_call():
 import pytest
 r=R('{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/other"}')
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
 for name in ("plan(","plan_bootstrap(","execute(","execute_bootstrap(","upload(","lifecycle(","deploy(","install_study("):
  start=s.index("def "+name); assert "require_expected_identity(r)" in s[start:start+500]

def test_plan_stack_parser_requires_application_parameters():
 s=_source(); assert 'q.add_argument("--domain-name",required=True)' in s and 'q.add_argument("--hosted-zone-id",required=True)' in s and 'q.add_argument("--certificate-email",required=True)' in s
def test_recover_study_access_parser_requires_exact_instance_confirmation():
 s=_source(); start=s.index('s.add_parser("recover-study-access")'); end=s.index('q=s.add_parser("plan-stack")'); section=s[start:end]
 assert 'q.add_argument("--confirm-instance",required=True)' in section
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
   if "get-caller-identity" in argv: return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv: return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"c"}}',"")
   if sum("get-command-invocation" in x for x in calls)==1: return subprocess.CompletedProcess(argv,255,"","InvocationDoesNotExist")
   if sum("get-command-invocation" in x for x in calls)==2: return subprocess.CompletedProcess(argv,0,'{"Status":"InProgress"}',"")
   return subprocess.CompletedProcess(argv,0,'{"Status":"Success"}',"")
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationBucketName":"b","DeployReleaseDocumentName":"d","ApplicationInstanceId":"i"})
 monkeypatch.setattr(cli.time,"sleep",lambda seconds:sleeps.append(seconds))
 cli.deploy(SequenceRunner(),"releases/a.tgz","a"*64,"b"*40,"epiagent.org","ops@example.org")
 assert sum("send-command" in call for call in calls)==1
 assert sum("get-command-invocation" in call for call in calls)==3
 assert len(sleeps)==2

def test_plan_rejects_access_denied_before_change_set(monkeypatch):
 import subprocess, pytest
 class Denied:
  def __init__(self): self.calls=[]
  def run(self,argv,*,capture_output=True):
   self.calls.append(argv)
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "describe-stacks" in argv and "epi-agent-bootstrap" in argv:return subprocess.CompletedProcess(argv,0,'{"Stacks":[{"Outputs":[{"OutputKey":"CloudFormationExecutionRoleArn","OutputValue":"r"}]}]}',"")
   return subprocess.CompletedProcess(argv,255,"","AccessDenied")
 r=Denied()
 with pytest.raises(cli.OperatorError): cli.plan(r,cli.APPLICATION_STACK,"template")
 assert not any("create-change-set" in call for call in r.calls)

def test_plan_requires_validation_error_and_nonexistence_phrase():
 s=_source(); assert '"does not exist" not in (probe.stderr or "").lower() or "validationerror" not in (probe.stderr or "").lower()' in s

def test_recover_study_access_uses_stack_document_without_parameters(monkeypatch,capsys):
 import subprocess
 calls=[]; events=[]
 class SequenceRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:
    events.append("send")
    return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"new-recovery-command"}}',"")
   if "get-command-invocation" in argv:
    events.append("poll")
    return subprocess.CompletedProcess(argv,0,'{"Status":"Success"}',"")
   raise AssertionError(argv)
 def recording_print(message,*,flush=False):
  events.append(("print",message,flush))
  builtins.print(message,flush=flush)
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 monkeypatch.setattr(cli,"print",recording_print,raising=False)
 cli.recover_study_access(SequenceRunner(),"i-0f9ed9c133ea2358b")
 sent=next(call for call in calls if "send-command" in call)
 assert sent[sent.index("--document-name")+1]=="epi-agent-recover-study-access"
 assert sent[sent.index("--instance-ids")+1]=="i-0f9ed9c133ea2358b"
 assert "--parameters" not in sent
 assert sum("send-command" in call for call in calls)==1
 polled=next(call for call in calls if "get-command-invocation" in call)
 assert polled[polled.index("--command-id")+1]=="new-recovery-command"
 assert events==["send",("print","study access recovery command ID: new-recovery-command",True),"poll"]
 assert capsys.readouterr().out=="study access recovery command ID: new-recovery-command\n"

def test_recover_study_access_checks_identity_before_outputs_and_send(monkeypatch):
 import subprocess
 events=[]
 class OrderedRunner:
  def run(self,argv,*,capture_output=True):
   if "get-caller-identity" in argv:
    events.append("identity")
    return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:
    events.append("send")
    return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"ordered-command"}}',"")
   if "get-command-invocation" in argv:return subprocess.CompletedProcess(argv,0,'{"Status":"Success"}',"")
   raise AssertionError(argv)
 def ordered_outputs(r):
  events.append("outputs")
  return {"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"}
 monkeypatch.setattr(cli,"outputs",ordered_outputs)
 cli.recover_study_access(OrderedRunner(),"i-0f9ed9c133ea2358b")
 assert events[:3]==["identity","outputs","send"]

def test_recover_study_access_rejects_wrong_instance_before_send(monkeypatch):
 import pytest, subprocess
 calls=[]
 class IdentityRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 with pytest.raises(cli.OperatorError,match="confirm the exact stack instance ID"):
  cli.recover_study_access(IdentityRunner(),"i-wrong")
 assert not any("send-command" in call for call in calls)

@pytest.mark.parametrize("status",["Failed","TimedOut","Cancelled"])
def test_recover_study_access_fails_closed_on_new_terminal_failure(monkeypatch,capsys,status):
 import subprocess
 calls=[]
 class FailedRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"failed-new-command"}}',"")
   if "get-command-invocation" in argv:return subprocess.CompletedProcess(argv,0,'{"Status":"'+status+'"}',"")
   raise AssertionError(argv)
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 with pytest.raises(cli.OperatorError,match="study access recovery command did not succeed"):
  cli.recover_study_access(FailedRunner(),"i-0f9ed9c133ea2358b")
 assert sum("send-command" in call for call in calls)==1
 assert sum("get-command-invocation" in call for call in calls)==1
 assert capsys.readouterr().out=="study access recovery command ID: failed-new-command\n"

def test_recover_study_access_times_out_without_resending(monkeypatch,capsys):
 import subprocess
 calls=[]; sleeps=[]; moments=iter((0,0,cli.DEPLOY_TIMEOUT_SECONDS+1))
 class TimeoutRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
   if "send-command" in argv:return subprocess.CompletedProcess(argv,0,'{"Command":{"CommandId":"timeout-command"}}',"")
   if "get-command-invocation" in argv:return subprocess.CompletedProcess(argv,0,'{"Status":"InProgress"}',"")
   raise AssertionError(argv)
 monkeypatch.setattr(cli,"outputs",lambda r:{"ApplicationInstanceId":"i-0f9ed9c133ea2358b","RecoverStudyAccessDocumentName":"epi-agent-recover-study-access"})
 monkeypatch.setattr(cli.time,"monotonic",lambda:next(moments))
 monkeypatch.setattr(cli.time,"sleep",lambda seconds:sleeps.append(seconds))
 with pytest.raises(cli.OperatorError,match="study access recovery command timeout-command timed out with last status InProgress"):
  cli.recover_study_access(TimeoutRunner(),"i-0f9ed9c133ea2358b")
 assert sum("send-command" in call for call in calls)==1
 assert sum("get-command-invocation" in call for call in calls)==1
 assert sleeps==[cli.POLL_INTERVAL_SECONDS]
 assert capsys.readouterr().out=="study access recovery command ID: timeout-command\n"

@pytest.mark.parametrize(
 ("key","sha","study_id","version"),
 [
  ("releases/study.tar.gz","a"*64,"study","1.0.0"),
  ("studies/../study.tar.gz","a"*64,"study","1.0.0"),
  ("studies/study.tar.gz","A"*64,"study","1.0.0"),
  ("studies/study.tar.gz","a"*63,"study","1.0.0"),
  ("studies/study.tar.gz","a"*64,"Study","1.0.0"),
  ("studies/study.tar.gz","a"*64,"study","version/one"),
 ],
)
def test_install_study_rejects_invalid_input_before_aws(key,sha,study_id,version):
 runner=R("{}")
 with pytest.raises(cli.OperatorError,match="invalid study installation input"):
  cli.install_study(runner,key,sha,study_id,version,"i-confirmed")
 assert runner.calls==[]

def test_install_study_parser_requires_exact_instance_confirmation():
 source=_source(); start=source.index('q=s.add_parser("install-study")'); end=source.index('q=s.add_parser("plan-stack")'); section=source[start:end]
 assert 'q.add_argument("key")' in section
 assert 'q.add_argument("sha")' in section
 assert 'q.add_argument("study_id")' in section
 assert 'q.add_argument("version")' in section
 assert 'q.add_argument("--confirm-instance",required=True)' in section

def _install_outputs():
 return {"ApplicationBucketName":"stack-bucket","ApplicationInstanceId":"i-stack","InstallStudyDocumentName":"epi-agent-install-study"}

def test_install_study_uses_stack_targets_and_verified_object(monkeypatch,capsys):
 import subprocess
 calls=[]; events=[]
 class SequenceRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv:
    events.append("identity"); payload='{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}'
   elif "head-object" in argv:
    events.append("head"); payload='{"Metadata":{"sha256":"'+"a"*64+'"}}'
   elif "send-command" in argv:
    events.append("send"); payload='{"Command":{"CommandId":"new-study-command"}}'
   elif "get-command-invocation" in argv:
    events.append("poll"); payload='{"Status":"Success"}'
   else: raise AssertionError(argv)
   return subprocess.CompletedProcess(argv,0,payload,"")
 def fixed_outputs(_runner): events.append("outputs"); return _install_outputs()
 monkeypatch.setattr(cli,"outputs",fixed_outputs)
 cli.install_study(SequenceRunner(),"studies/report-india-synthetic-0.3.0.tar.gz","a"*64,"report-india-synthetic","0.3.0","i-stack")
 head=next(call for call in calls if "head-object" in call)
 assert head[head.index("--bucket")+1]=="stack-bucket"
 assert head[head.index("--key")+1]=="studies/report-india-synthetic-0.3.0.tar.gz"
 sent=next(call for call in calls if "send-command" in call)
 assert sent[sent.index("--document-name")+1]=="epi-agent-install-study"
 assert sent[sent.index("--instance-ids")+1]=="i-stack"
 assert json.loads(sent[sent.index("--parameters")+1])=={"Bucket":["stack-bucket"],"StudyKey":["studies/report-india-synthetic-0.3.0.tar.gz"],"StudySha256":["a"*64],"StudyId":["report-india-synthetic"],"PackageVersion":["0.3.0"]}
 polled=next(call for call in calls if "get-command-invocation" in call)
 assert polled[polled.index("--command-id")+1]=="new-study-command"
 assert events==["identity","outputs","head","send","poll"]
 assert capsys.readouterr().out=="study installation command ID: new-study-command\n"

def test_install_study_rejects_wrong_instance_before_object_or_send(monkeypatch):
 import subprocess
 calls=[]
 class IdentityRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv)); return subprocess.CompletedProcess(argv,0,'{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}',"")
 monkeypatch.setattr(cli,"outputs",lambda _runner:_install_outputs())
 with pytest.raises(cli.OperatorError,match="confirm the exact stack instance ID"):
  cli.install_study(IdentityRunner(),"studies/report-india-synthetic-0.3.0.tar.gz","a"*64,"report-india-synthetic","0.3.0","i-wrong")
 assert not any("head-object" in call or "send-command" in call for call in calls)

@pytest.mark.parametrize("metadata",[{}, {"sha256":"b"*64}])
def test_install_study_rejects_missing_or_mismatched_object_checksum(monkeypatch,metadata):
 import subprocess
 calls=[]
 class MetadataRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv: payload='{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}'
   elif "head-object" in argv: payload=json.dumps({"Metadata":metadata})
   else: raise AssertionError(argv)
   return subprocess.CompletedProcess(argv,0,payload,"")
 monkeypatch.setattr(cli,"outputs",lambda _runner:_install_outputs())
 with pytest.raises(cli.OperatorError,match="study object checksum metadata does not match"):
  cli.install_study(MetadataRunner(),"studies/report-india-synthetic-0.3.0.tar.gz","a"*64,"report-india-synthetic","0.3.0","i-stack")
 assert not any("send-command" in call for call in calls)

@pytest.mark.parametrize("status",["Failed","TimedOut","Cancelled"])
def test_install_study_fails_closed_on_terminal_failure(monkeypatch,capsys,status):
 import subprocess
 calls=[]
 class FailedRunner:
  def run(self,argv,*,capture_output=True):
   calls.append(list(argv))
   if "get-caller-identity" in argv: payload='{"Account":"641379499556","Arn":"arn:aws:iam::641379499556:user/xutao-dev"}'
   elif "head-object" in argv: payload='{"Metadata":{"sha256":"'+"a"*64+'"}}'
   elif "send-command" in argv: payload='{"Command":{"CommandId":"failed-study-command"}}'
   elif "get-command-invocation" in argv: payload='{"Status":"'+status+'"}'
   else: raise AssertionError(argv)
   return subprocess.CompletedProcess(argv,0,payload,"")
 monkeypatch.setattr(cli,"outputs",lambda _runner:_install_outputs())
 with pytest.raises(cli.OperatorError,match="study installation command did not succeed"):
  cli.install_study(FailedRunner(),"studies/report-india-synthetic-0.3.0.tar.gz","a"*64,"report-india-synthetic","0.3.0","i-stack")
 assert sum("send-command" in call for call in calls)==1
 assert sum("get-command-invocation" in call for call in calls)==1
 assert capsys.readouterr().out=="study installation command ID: failed-study-command\n"
