#!/usr/bin/env python3
"""Explicitly opt-in, real AWS Phase 2A acceptance smoke; never run by pytest."""
from __future__ import annotations
import argparse, os, sys
from urllib.parse import urlparse
PHASES=("tls","http_redirect","cognito_login","provider_key","owner_isolation","restart_persistence","stop_start","snapshot_restore")
DEFAULT=("tls","http_redirect","cognito_login","provider_key","owner_isolation")
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
 selected=list(DEFAULT)+[x for x in ("restart_persistence","stop_start","snapshot_restore") if getattr(args,x)]
 for phase in selected: print("live smoke phase:",phase)
 return 0
def main(argv=None):
 try:return run(parse_args(argv))
 except ValueError as error: print("error:",error,file=sys.stderr); return 2
if __name__=="__main__":raise SystemExit(main())
