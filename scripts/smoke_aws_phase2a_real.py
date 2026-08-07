#!/usr/bin/env python3
"""Explicitly opt-in, real AWS Phase 2A acceptance smoke; never run by pytest."""
from __future__ import annotations
import argparse, os, sys, ssl, json, uuid
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from urllib.error import HTTPError
PHASES=("tls","http_redirect","cognito_login","provider_key","owner_isolation","restart_persistence","stop_start","snapshot_restore")
DEFAULT=("tls","http_redirect","cognito_login","provider_key","owner_isolation")
PROVIDER_KEY_ENV="REPORT_AGENT_SMOKE_PROVIDER_KEY"
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args): return None
class RequestError(ValueError):
 def __init__(self,status,message): super().__init__(message); self.status=status
def safe_error(error, secrets):
 text=str(error)
 for secret in secrets: text=text.replace(secret,"[REDACTED]")
 return text
def request_text(url, *, method="GET", headers=None, body=None, secrets=()):
 try:
  request=Request(url,data=body,headers=headers or {},method=method)
  with urlopen(request,timeout=15,context=ssl.create_default_context()) as response:return response.read().decode("utf-8")
 except HTTPError as error: raise RequestError(error.code,safe_error(error,secrets)) from None
 except Exception as error: raise RequestError(None,safe_error(error,secrets)) from None
def request_json(url, **kwargs): return json.loads(request_text(url,**kwargs))
def auth_headers(token, session_id): return {"Authorization":"Bearer "+token,"X-Epi-Session-ID":session_id}
def login(base_url, credential, secrets, session_id=None):
 session_id=session_id or str(uuid.uuid4()); request_json(base_url+"/api/session/provider-key",headers=auth_headers(credential,session_id),secrets=secrets)
 return {"token":credential,"session_id":session_id}
def provider_key(base_url, session, secret, secrets, session_id=""):
 result=request_json(base_url+"/api/session/provider-key",method="PUT",headers={**auth_headers(session,session_id),"Content-Type":"application/json"},body=json.dumps({"api_key":secret}).encode(),secrets=secrets)
 if result.get("configured") is not True: raise ValueError("provider key was not configured")
 return result
def owner_isolation(base_url, first, second, secrets):
 a=request_json(base_url+"/api/threads",method="POST",headers={"Authorization":"Bearer "+first},body=b"{}",secrets=secrets)
 b=request_json(base_url+"/api/threads",method="POST",headers={"Authorization":"Bearer "+second},body=b"{}",secrets=secrets)
 if a.get("thread_id")==b.get("thread_id"): raise ValueError("owner isolation failed")
 try: request_json(base_url+"/api/threads/"+a["thread_id"]+"/state",headers={"Authorization":"Bearer "+second},secrets=secrets)
 except RequestError as error:
  if error.status==404: return
  raise
 raise ValueError("cross-owner state request was not denied")
def check_https(base_url):
 request=Request(base_url,method="HEAD")
 with urlopen(request,timeout=15,context=ssl.create_default_context()) as response:
  if response.geturl().rstrip("/")!=base_url.rstrip("/"): raise ValueError("HTTPS target redirected unexpectedly")
def check_redirect(base_url):
 request=Request("http://epiagent.org",method="HEAD")
 try: build_opener(NoRedirect).open(request,timeout=15)
 except Exception as error:
  location=getattr(error,"headers",{}).get("Location","")
  if location.rstrip("/")!=base_url.rstrip("/"): raise ValueError("HTTP does not redirect to canonical HTTPS URL")
def parse_args(argv=None):
 p=argparse.ArgumentParser(description=__doc__); p.add_argument("--base-url",required=True); p.add_argument("--allow-live-aws",action="store_true")
 p.add_argument("--user-one-env",required=True); p.add_argument("--user-two-env",required=True)
 for phase in PHASES:
  if phase not in DEFAULT:p.add_argument("--"+phase.replace("_","-"),action="store_true")
 p.add_argument("--instance-id",default=""); p.add_argument("--snapshot-id",default="")
 return p.parse_args(argv)
def run(args):
 url=urlparse(args.base_url)
 if not args.allow_live_aws: raise ValueError("--allow-live-aws is required")
 if url.scheme!="https" or url.hostname!="epiagent.org" or url.path not in ("", "/"): raise ValueError("base URL must be https://epiagent.org")
 if args.user_one_env==args.user_two_env: raise ValueError("two distinct credential environment variable names are required")
 if not os.environ.get(args.user_one_env) or not os.environ.get(args.user_two_env): raise ValueError("required test-user credentials are absent")
 secrets=[os.environ[args.user_one_env],os.environ[args.user_two_env]]
 if "provider_key" in DEFAULT and not os.environ.get(PROVIDER_KEY_ENV): raise ValueError("required provider-key environment variable is absent")
 provider_secret=os.environ[PROVIDER_KEY_ENV]; secrets.append(provider_secret)
 selected=list(DEFAULT)+[x for x in ("restart_persistence","stop_start","snapshot_restore") if getattr(args,x)]
 for phase in selected:
  print("live smoke phase:",phase)
  try:
   if phase=="tls": check_https(args.base_url)
   elif phase=="http_redirect": check_redirect(args.base_url)
   elif phase=="cognito_login": sessions=[login(args.base_url,secrets[0],secrets),login(args.base_url,secrets[1],secrets)]
   elif phase=="provider_key": provider_key(args.base_url,sessions[0].get("token",""),provider_secret,secrets,sessions[0]["session_id"])
   elif phase=="owner_isolation": owner_isolation(args.base_url,sessions[0].get("token",""),sessions[1].get("token",""),secrets)
   elif phase in {"restart_persistence","stop_start"}:
    if not args.instance_id: raise ValueError("--instance-id is required for disruptive phase")
    raise ValueError("instance "+args.instance_id+" requires separate confirmed operator action")
   elif phase=="snapshot_restore":
    if not args.snapshot_id: raise ValueError("--snapshot-id is required for restore")
    raise ValueError("snapshot "+args.snapshot_id+" requires separate confirmed operator action")
  except Exception as error: raise ValueError(safe_error(error,secrets)) from None
 return 0
def main(argv=None):
 try:return run(parse_args(argv))
 except ValueError as error: print("error:",error,file=sys.stderr); return 2
if __name__=="__main__":raise SystemExit(main())
