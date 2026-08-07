#!/usr/bin/env python3
"""Account-guarded operator commands for the Epi Agent Phase 2A stack."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys
from pathlib import Path
from typing import Protocol, Sequence

EXPECTED_ACCOUNT="641379499556"; EXPECTED_PROFILE="xutao-dev"; EXPECTED_REGION="us-east-1"
BOOTSTRAP_STACK="epi-agent-bootstrap"; APPLICATION_STACK="epi-agent-phase2a"
class OperatorError(RuntimeError): pass
class Runner(Protocol):
 def run(self, argv: Sequence[str], *, capture_output: bool=True) -> subprocess.CompletedProcess[str]: ...
class SubprocessRunner:
 def run(self, argv, *, capture_output=True): return subprocess.run(argv,text=True,capture_output=capture,check=False)
def aws(*args:str)->list[str]: return ["aws",*args,"--profile",EXPECTED_PROFILE,"--region",EXPECTED_REGION]
def run_json(r:Runner, argv:Sequence[str])->dict:
 p=r.run(argv)
 if p.returncode: raise OperatorError(p.stderr or "AWS command failed")
 try:return json.loads(p.stdout)
 except ValueError as e: raise OperatorError("AWS returned invalid JSON") from e
def require_expected_identity(r:Runner)->dict[str,str]:
 i=run_json(r,aws("sts","get-caller-identity")); arn=i.get("Arn","")
 if i.get("Account")!=EXPECTED_ACCOUNT or f":{EXPECTED_ACCOUNT}:" not in arn: raise OperatorError("refusing AWS mutation: unexpected account")
 return i
def bootstrap_role(r:Runner)->str:
 o=run_json(r,aws("cloudformation","describe-stacks","--stack-name",BOOTSTRAP_STACK))
 for x in o["Stacks"][0]["Outputs"]:
  if x["OutputKey"]=="CloudFormationExecutionRoleArn": return x["OutputValue"]
 raise OperatorError("bootstrap execution role output missing")
def stack_args(r:Runner,*x:str)->list[str]: return aws("cloudformation",*x,"--role-arn",bootstrap_role(r))
def outputs(r:Runner)->dict[str,str]:
 o=run_json(r,stack_args(r,"describe-stacks","--stack-name",APPLICATION_STACK)); return {x["OutputKey"]:x["OutputValue"] for x in o["Stacks"][0]["Outputs"]}
def change_name(stack:str)->str:return f"epi-agent-{stack}-plan"
def plan(r:Runner,stack:str,template:str)->dict:
 require_expected_identity(r); role=bootstrap_role(r); name=change_name(stack)
 run_json(r,aws("cloudformation","create-change-set","--stack-name",stack,"--change-set-name",name,"--change-set-type","CREATE","--template-body",f"file://{template}","--role-arn",role))
 result=run_json(r,aws("cloudformation","describe-change-set","--stack-name",stack,"--change-set-name",name,"--role-arn",role)); print(json.dumps(result,sort_keys=True)); return result
def execute(r:Runner,arn:str,confirm:str)->None:
 if confirm!=EXPECTED_ACCOUNT: raise OperatorError("confirm the expected account exactly")
 require_expected_identity(r); d=run_json(r,stack_args(r,"describe-change-set","--change-set-name",arn))
 if d.get("ChangeSetArn")!=arn or d.get("StackName")!=APPLICATION_STACK or d.get("Status")!="CREATE_COMPLETE": raise OperatorError("refusing unexpected change set")
 p=r.run(stack_args(r,"execute-change-set","--change-set-name",arn));
 if p.returncode: raise OperatorError(p.stderr)
def upload(r:Runner,file:str,key:str)->None:
 require_expected_identity(r); p=Path(file); digest=hashlib.sha256(p.read_bytes()).hexdigest(); size=str(p.stat().st_size); bucket=outputs(r)["ApplicationBucketName"]
 head=r.run(aws("s3api","head-object","--bucket",bucket,"--key",key))
 if not head.returncode:
  old=json.loads(head.stdout).get("Metadata",{}).get("sha256")
  if old!=digest: raise OperatorError("refusing overwrite with different checksum")
  return
 cp=r.run(aws("s3","cp",str(p),f"s3://{bucket}/{key}","--metadata",f"sha256={digest}","--expected-size",size))
 if cp.returncode: raise OperatorError(cp.stderr)
def main(argv=None, runner:Runner|None=None)->int:
 r=runner or SubprocessRunner(); p=argparse.ArgumentParser(); s=p.add_subparsers(dest="cmd",required=True)
 s.add_parser("identity"); s.add_parser("validate");
 for n in ("plan-bootstrap","execute-bootstrap","plan-stack","outputs","stop","start","deploy-release","upload-release","upload-study","execute-change-set"): s.add_parser(n)
 a=p.parse_args(argv)
 try:
  if a.cmd=="identity": print(json.dumps(require_expected_identity(r))); return 0
  if a.cmd=="validate":
   q=r.run(["uvx","--from","cfn-lint==1.53.1","cfn-lint","infra/aws/bootstrap/template.yaml","infra/aws/phase2a/template.yaml"],capture_output=False); return q.returncode
  if a.cmd=="outputs": print(json.dumps(outputs(r),sort_keys=True)); return 0
  raise OperatorError("subcommand requires explicit operator arguments")
 except OperatorError as e: print(f"error: {e}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
