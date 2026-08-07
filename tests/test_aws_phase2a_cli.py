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
