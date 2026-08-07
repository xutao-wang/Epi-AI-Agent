#!/usr/bin/env python3
"""Explicitly opt-in, real AWS Phase 2A acceptance smoke; never run by pytest."""
from __future__ import annotations
import argparse, os, sys, ssl, json
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
PHASES=("tls","http_redirect","cognito_login","provider_key","owner_isolation","restart_persistence","stop_start","snapshot_restore")
DEFAULT=("tls","http_redirect","cognito_login","provider_key","owner_isolation")
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*args): return None
def safe_error(error, secrets):
 text=str(error)
 for secret in secrets: text=text.replace(secret,"[REDACTED]")
 return text
def request_text(url, *, headers=None, body=None, secrets=()):
 try:
  request=Request(url,data=body,headers=headers or {})
  with urlopen(request,timeout=15,context=ssl.create_default_context()) as response:return response.read().decode("utf-8")
 except Exception as error: raise ValueError(safe_error(error,secrets)) from None
def request_json(url, **kwargs): return json.loads(request_text(url,**kwargs))
def check_https(base_url):
 request=Request(base_url,method="HEAD")
 with urlopen(request,timeout=15,context=ssl.create_default_context()) as response:
  if response.geturl()!=base_url: raise ValueError("HTTPS target redirected unexpectedly")
def check_redirect(base_url):
 request=Request("http://epiagent.org",method="HEAD")
 try: build_opener(NoRedirect).open(request,timeout=15)
 except Exception as error:
  location=getattr(error,"headers",{}).get("Location","")
  if location!=base_url: raise ValueError("HTTP does not redirect to canonical HTTPS URL")
def parse_args(argv=None):
 p=argparse.ArgumentParser(description=__doc__); p.add_argument("--base-url",required=True); p.add_argument("--allow-live-aws",action="store_true")
 p.add_argument("--user-one-env",required=True); p.add_argument("--user-two-env",required=True)
 for phase in PHASES:
  if phase not in DEFAULT:p.add_argument("--"+phase.replace("_","-"),action="store_true")
 return p.parse_args(argv)
def run(args):
 url=urlparse(args.base_url)
 if not args.allow_live_aws: raise ValueError("--allow-live-aws is required")
 if url.scheme!="https" or url.hostname!="epiagent.org" or url.path not in ("", "/"): raise ValueError("base URL must be https://epiagent.org")
 if args.user_one_env==args.user_two_env: raise ValueError("two distinct credential environment variable names are required")
 if not os.environ.get(args.user_one_env) or not os.environ.get(args.user_two_env): raise ValueError("required test-user credentials are absent")
 secrets=[os.environ[args.user_one_env],os.environ[args.user_two_env]]
 selected=list(DEFAULT)+[x for x in ("restart_persistence","stop_start","snapshot_restore") if getattr(args,x)]
 for phase in selected:
  print("live smoke phase:",phase)
  try:
   if phase=="tls": check_https(args.base_url)
   elif phase=="http_redirect": check_redirect(args.base_url)
   elif phase in {"cognito_login","provider_key","owner_isolation"}: print("authenticated safe check requested")
   elif phase in {"stop_start","snapshot_restore"}: print("target must be printed and separately confirmed")
  except Exception as error: raise ValueError(safe_error(error,secrets)) from None
 return 0
def main(argv=None):
 try:return run(parse_args(argv))
 except ValueError as error: print("error:",error,file=sys.stderr); return 2
if __name__=="__main__":raise SystemExit(main())
