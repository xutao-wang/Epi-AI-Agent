#!/usr/bin/env python3
"""Account-guarded operator commands for the Epi Agent Phase 2A stack."""
from __future__ import annotations
import argparse, hashlib, json, re, subprocess, sys, time, uuid
from pathlib import Path
from typing import Protocol, Sequence

EXPECTED_ACCOUNT="641379499556"; EXPECTED_PROFILE="xutao-dev"; EXPECTED_REGION="us-east-1"
EXPECTED_PRINCIPAL_ARN="arn:aws:iam::641379499556:user/xutao-dev"
BOOTSTRAP_STACK="epi-agent-bootstrap"; APPLICATION_STACK="epi-agent-phase2a"; WWW_DNS_STACK="epi-agent-www-dns"
POLL_INTERVAL_SECONDS=1
CHANGE_SET_TIMEOUT_SECONDS=1800
DEPLOY_TIMEOUT_SECONDS=3600
STUDY_KEY_PATTERN=re.compile(r"^studies/[A-Za-z0-9][A-Za-z0-9._-]*\.tar\.gz$")
STUDY_TOKEN_PATTERN=re.compile(r"^[a-z0-9][a-z0-9._-]*$")
class OperatorError(RuntimeError): pass
class Runner(Protocol):
 def run(self, argv: Sequence[str], *, capture_output: bool=True) -> subprocess.CompletedProcess[str]: ...
class SubprocessRunner:
 def run(self, argv, *, capture_output=True): return subprocess.run(argv,text=True,capture_output=capture_output,check=False)
def aws(*args:str)->list[str]: return ["aws",*args,"--profile",EXPECTED_PROFILE,"--region",EXPECTED_REGION,"--output","json"]
def run_json(r:Runner, argv:Sequence[str])->dict:
 p=r.run(argv)
 if p.returncode: raise OperatorError(p.stderr or "AWS command failed")
 try:return json.loads(p.stdout)
 except ValueError as e: raise OperatorError("AWS returned invalid JSON") from e
def require_expected_identity(r:Runner)->dict[str,str]:
 i=run_json(r,aws("sts","get-caller-identity")); arn=i.get("Arn","")
 parts=arn.split(":")
 if i.get("Account")!=EXPECTED_ACCOUNT or arn!=EXPECTED_PRINCIPAL_ARN or len(parts)!=6 or parts[0]!="arn" or parts[1]!="aws" or parts[4]!=EXPECTED_ACCOUNT: raise OperatorError("refusing AWS mutation: unexpected account")
 return i
def bootstrap_role(r:Runner)->str:
 o=run_json(r,aws("cloudformation","describe-stacks","--stack-name",BOOTSTRAP_STACK))
 for x in o["Stacks"][0]["Outputs"]:
  if x["OutputKey"]=="CloudFormationExecutionRoleArn": return x["OutputValue"]
 raise OperatorError("bootstrap execution role output missing")
def stack_args(r:Runner,*x:str)->list[str]: return aws("cloudformation",*x)
def outputs(r:Runner)->dict[str,str]:
 o=run_json(r,stack_args(r,"describe-stacks","--stack-name",APPLICATION_STACK)); return {x["OutputKey"]:x["OutputValue"] for x in o["Stacks"][0]["Outputs"]}
def change_name(stack:str)->str:return f"epi-agent-{stack}-plan-{uuid.uuid4().hex[:12]}"
def plan(r:Runner,stack:str,template:str, parameters:list[str]|None=None)->dict:
 require_expected_identity(r); role=bootstrap_role(r); name=change_name(stack)
 probe=r.run(aws("cloudformation","describe-stacks","--stack-name",stack))
 if probe.returncode and ("does not exist" not in (probe.stderr or "").lower() or "validationerror" not in (probe.stderr or "").lower()): raise OperatorError(probe.stderr or "unable to describe stack")
 exists=probe.returncode==0
 extra=["--parameters",*parameters] if parameters else []
 run_json(r,aws("cloudformation","create-change-set","--stack-name",stack,"--change-set-name",name,"--change-set-type","UPDATE" if exists else "CREATE","--template-body",f"file://{template}","--role-arn",role,"--capabilities","CAPABILITY_NAMED_IAM",*extra))
 deadline=time.monotonic()+CHANGE_SET_TIMEOUT_SECONDS
 while time.monotonic()<deadline:
  result=run_json(r,aws("cloudformation","describe-change-set","--stack-name",stack,"--change-set-name",name))
  if result.get("Status") in {"CREATE_COMPLETE","FAILED"}:
   print(json.dumps(result,sort_keys=True))
   if result["Status"]=="FAILED": raise OperatorError("change set creation failed")
   return result
  time.sleep(POLL_INTERVAL_SECONDS)
 raise OperatorError("change set did not reach a terminal status before timeout")
def plan_bootstrap(r:Runner,template:str)->dict:
 require_expected_identity(r); name=change_name(BOOTSTRAP_STACK)
 run_json(r,aws("cloudformation","create-change-set","--stack-name",BOOTSTRAP_STACK,"--change-set-name",name,"--change-set-type","CREATE","--template-body",f"file://{template}","--capabilities","CAPABILITY_NAMED_IAM"))
 deadline=time.monotonic()+CHANGE_SET_TIMEOUT_SECONDS
 while time.monotonic()<deadline:
  result=run_json(r,aws("cloudformation","describe-change-set","--stack-name",BOOTSTRAP_STACK,"--change-set-name",name))
  if result.get("Status") in {"CREATE_COMPLETE","FAILED"}:
   print(json.dumps(result,sort_keys=True))
   if result["Status"]=="FAILED": raise OperatorError("change set creation failed")
   return result
  time.sleep(POLL_INTERVAL_SECONDS)
 raise OperatorError("change set did not reach a terminal status before timeout")
def execute(r:Runner,arn:str,confirm:str,stack:str=APPLICATION_STACK)->None:
 if confirm!=EXPECTED_ACCOUNT: raise OperatorError("confirm the expected account exactly")
 require_expected_identity(r); d=run_json(r,stack_args(r,"describe-change-set","--change-set-name",arn))
 if d.get("ChangeSetId",d.get("ChangeSetArn"))!=arn or d.get("StackName")!=stack or d.get("Status")!="CREATE_COMPLETE": raise OperatorError("refusing unexpected change set")
 p=r.run(aws("cloudformation","execute-change-set","--change-set-name",arn));
 if p.returncode: raise OperatorError(p.stderr)
def execute_bootstrap(r:Runner,arn:str,confirm:str)->None:
 if confirm!=EXPECTED_ACCOUNT: raise OperatorError("confirm the expected account exactly")
 require_expected_identity(r); d=run_json(r,aws("cloudformation","describe-change-set","--change-set-name",arn))
 if d.get("ChangeSetId",d.get("ChangeSetArn"))!=arn or d.get("StackName")!=BOOTSTRAP_STACK or d.get("Status")!="CREATE_COMPLETE": raise OperatorError("refusing unexpected change set")
 p=r.run(aws("cloudformation","execute-change-set","--change-set-name",arn))
 if p.returncode: raise OperatorError(p.stderr)
def upload(r:Runner,file:str,key:str)->None:
 require_expected_identity(r); p=Path(file); digest=hashlib.sha256(p.read_bytes()).hexdigest(); size=str(p.stat().st_size); bucket=outputs(r)["ApplicationBucketName"]
 head=r.run(aws("s3api","head-object","--bucket",bucket,"--key",key))
 if not head.returncode:
  old=json.loads(head.stdout).get("Metadata",{}).get("sha256")
  if old!=digest: raise OperatorError("refusing overwrite with different checksum")
  return
 if head.returncode and "404" not in (head.stderr or "") and "Not Found" not in (head.stderr or ""): raise OperatorError(head.stderr or "unable to inspect artifact")
 cp=r.run(aws("s3","cp",str(p),f"s3://{bucket}/{key}","--metadata",f"sha256={digest}","--expected-size",size))
 if cp.returncode: raise OperatorError(cp.stderr)
def lifecycle(r:Runner, action:str, confirm:str)->None:
 require_expected_identity(r); instance=outputs(r).get("ApplicationInstanceId","")
 if not instance or confirm!=instance: raise OperatorError("confirm the exact stack instance ID")
 print("stop preserves EBS cost but removes availability" if action=="stop" else "start restores availability and EC2 cost")
 p=r.run(aws("ec2",action+"-instances","--instance-ids",instance))
 if p.returncode: raise OperatorError(p.stderr)
def wait_for_ssm_command(r:Runner,command:str,instance:str,operation:str)->None:
 deadline=time.monotonic()+DEPLOY_TIMEOUT_SECONDS; last_status="pending"
 while time.monotonic()<deadline:
  try: result=run_json(r,aws("ssm","get-command-invocation","--command-id",command,"--instance-id",instance))
  except OperatorError as error:
   if "InvocationDoesNotExist" in str(error): time.sleep(POLL_INTERVAL_SECONDS); continue
   raise
  status=result.get("Status"); last_status=status or last_status
  if status=="Success": return
  if status in {"Failed","TimedOut","Cancelled"}: raise OperatorError(f"{operation} command did not succeed")
  time.sleep(POLL_INTERVAL_SECONDS)
 raise OperatorError(f"{operation} command {command} timed out with last status {last_status}")
def deploy(r:Runner,key:str,sha:str,release_id:str,domain:str,email:str)->None:
 if not key.startswith("releases/") or ".." in key or not re.fullmatch(r"[0-9a-f]{64}",sha) or not re.fullmatch(r"[0-9a-f]{40}",release_id) or not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+",domain) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): raise OperatorError("invalid release deployment input")
 require_expected_identity(r); o=outputs(r); document=o.get("DeployReleaseDocumentName",""); instance=o.get("ApplicationInstanceId","")
 if not document or not instance: raise OperatorError("required deployment outputs missing")
 params=json.dumps({"Bucket":[o["ApplicationBucketName"]],"ReleaseKey":[key],"ReleaseSha256":[sha],"ReleaseId":[release_id],"DomainName":[domain],"CertificateEmail":[email]})
 sent=run_json(r,aws("ssm","send-command","--document-name",document,"--instance-ids",instance,"--parameters",params)); command=sent["Command"]["CommandId"]
 wait_for_ssm_command(r,command,instance,"deployment")
def recover_study_access(r:Runner,confirm:str)->None:
 require_expected_identity(r); o=outputs(r); document=o.get("RecoverStudyAccessDocumentName",""); instance=o.get("ApplicationInstanceId","")
 if not document or not instance: raise OperatorError("required recovery outputs missing")
 if confirm!=instance: raise OperatorError("confirm the exact stack instance ID")
 sent=run_json(r,aws("ssm","send-command","--document-name",document,"--instance-ids",instance)); command=sent["Command"]["CommandId"]
 print(f"study access recovery command ID: {command}",flush=True)
 wait_for_ssm_command(r,command,instance,"study access recovery")
def install_study(r:Runner,key:str,sha:str,study_id:str,version:str,confirm:str)->None:
 if not STUDY_KEY_PATTERN.fullmatch(key) or ".." in key or not re.fullmatch(r"[0-9a-f]{64}",sha) or not STUDY_TOKEN_PATTERN.fullmatch(study_id) or not STUDY_TOKEN_PATTERN.fullmatch(version): raise OperatorError("invalid study installation input")
 require_expected_identity(r); o=outputs(r); bucket=o.get("ApplicationBucketName",""); instance=o.get("ApplicationInstanceId",""); document=o.get("InstallStudyDocumentName","")
 if not bucket or not instance or not document: raise OperatorError("required study installation outputs missing")
 if confirm!=instance: raise OperatorError("confirm the exact stack instance ID")
 remote=run_json(r,aws("s3api","head-object","--bucket",bucket,"--key",key))
 if (remote.get("Metadata") or {}).get("sha256")!=sha: raise OperatorError("study object checksum metadata does not match")
 params=json.dumps({"Bucket":[bucket],"StudyKey":[key],"StudySha256":[sha],"StudyId":[study_id],"PackageVersion":[version]})
 sent=run_json(r,aws("ssm","send-command","--document-name",document,"--instance-ids",instance,"--parameters",params)); command=sent["Command"]["CommandId"]
 print(f"study installation command ID: {command}",flush=True)
 wait_for_ssm_command(r,command,instance,"study installation")
def main(argv=None, runner:Runner|None=None)->int:
 r=runner or SubprocessRunner(); p=argparse.ArgumentParser(); s=p.add_subparsers(dest="cmd",required=True)
 s.add_parser("identity"); s.add_parser("validate"); s.add_parser("outputs")
 s.add_parser("plan-bootstrap")
 for n in ("execute-bootstrap","execute-change-set","execute-www-dns-change-set"):
  q=s.add_parser(n); q.add_argument("change_set_arn"); q.add_argument("--confirm-account",required=True)
 for n,prefix in (("upload-release","releases/"),("upload-study","studies/")):
  q=s.add_parser(n); q.add_argument("file"); q.add_argument("key"); q.set_defaults(prefix=prefix)
 for n in ("stop","start"):
  q=s.add_parser(n); q.add_argument("--confirm-instance",required=True)
 q=s.add_parser("recover-study-access"); q.add_argument("--confirm-instance",required=True)
 q=s.add_parser("install-study"); q.add_argument("key"); q.add_argument("sha"); q.add_argument("study_id"); q.add_argument("version"); q.add_argument("--confirm-instance",required=True)
 q=s.add_parser("plan-www-dns"); q.add_argument("--domain-name",required=True); q.add_argument("--hosted-zone-id",required=True)
 q=s.add_parser("plan-stack"); q.add_argument("--domain-name",required=True); q.add_argument("--hosted-zone-id",required=True); q.add_argument("--certificate-email",required=True); q.add_argument("--alert-email",default=""); q.add_argument("--data-volume-gib",default="50"); q.add_argument("--data-snapshot-id",default="")
 q=s.add_parser("deploy-release"); q.add_argument("key"); q.add_argument("sha"); q.add_argument("release_id"); q.add_argument("domain"); q.add_argument("email")
 a=p.parse_args(argv)
 try:
  if a.cmd=="identity": print(json.dumps(require_expected_identity(r))); return 0
  if a.cmd=="validate":
   root=Path(__file__).resolve().parents[1]; q=r.run(["uvx","--from","cfn-lint==1.53.1","cfn-lint",str(root/"infra/aws/bootstrap/template.yaml"),str(root/"infra/aws/phase2a/template.yaml"),str(root/"infra/aws/www-dns/template.yaml")],capture_output=False)
   if q.returncode:return q.returncode
   for template in (root/"infra/aws/phase2a/template.yaml",root/"infra/aws/www-dns/template.yaml"): run_json(r,aws("cloudformation","validate-template","--template-body",f"file://{template}"))
   return 0
  if a.cmd=="outputs": print(json.dumps(outputs(r),sort_keys=True)); return 0
  if a.cmd=="plan-bootstrap": plan_bootstrap(r,str(Path(__file__).resolve().parents[1]/"infra/aws/bootstrap/template.yaml")); return 0
  if a.cmd=="plan-stack":
   if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+",a.domain_name) or not re.fullmatch(r"Z[A-Z0-9]+",a.hosted_zone_id) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",a.certificate_email): raise OperatorError("invalid stack parameter")
   vals={"DomainName":a.domain_name,"HostedZoneId":a.hosted_zone_id,"CertificateEmail":a.certificate_email,"AlertEmail":a.alert_email,"DataVolumeGiB":a.data_volume_gib,"DataSnapshotId":a.data_snapshot_id}
   plan(r,APPLICATION_STACK,str(Path(__file__).resolve().parents[1]/"infra/aws/phase2a/template.yaml"),[f"ParameterKey={k},ParameterValue={v}" for k,v in vals.items()]); return 0
  if a.cmd=="plan-www-dns":
   if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+",a.domain_name) or not re.fullmatch(r"Z[A-Z0-9]+",a.hosted_zone_id): raise OperatorError("invalid DNS stack parameter")
   vals={"DomainName":a.domain_name,"HostedZoneId":a.hosted_zone_id}
   plan(r,WWW_DNS_STACK,str(Path(__file__).resolve().parents[1]/"infra/aws/www-dns/template.yaml"),[f"ParameterKey={k},ParameterValue={v}" for k,v in vals.items()]); return 0
  if a.cmd=="execute-bootstrap": execute_bootstrap(r,a.change_set_arn,a.confirm_account); return 0
  if a.cmd=="execute-change-set": execute(r,a.change_set_arn,a.confirm_account); return 0
  if a.cmd=="execute-www-dns-change-set": execute(r,a.change_set_arn,a.confirm_account,WWW_DNS_STACK); return 0
  if a.cmd.startswith("upload-"):
   if not a.key.startswith(a.prefix) or ".." in a.key: raise OperatorError("unsafe artifact key")
   upload(r,a.file,a.key); return 0
  if a.cmd in ("stop","start"): lifecycle(r,a.cmd,a.confirm_instance); return 0
  if a.cmd=="recover-study-access": recover_study_access(r,a.confirm_instance); return 0
  if a.cmd=="install-study": install_study(r,a.key,a.sha,a.study_id,a.version,a.confirm_instance); return 0
  if a.cmd=="deploy-release": deploy(r,a.key,a.sha,a.release_id,a.domain,a.email); return 0
  raise OperatorError("subcommand requires explicit operator arguments")
 except OperatorError as e: print(f"error: {e}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
