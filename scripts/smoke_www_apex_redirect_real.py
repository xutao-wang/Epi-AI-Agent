#!/usr/bin/env python3.12
"""Run the www-to-apex production redirect smoke exactly once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import traceback
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


CANONICAL_ORIGIN = "https://epiagent.org"
WWW_ORIGIN = "https://www.epiagent.org"
SMOKE_QUERY = "?domain_redirect_smoke=www"
REDIRECT_CASES = (
    (f"http://www.epiagent.org/{SMOKE_QUERY}", f"{CANONICAL_ORIGIN}/{SMOKE_QUERY}"),
    (f"{WWW_ORIGIN}/{SMOKE_QUERY}", f"{CANONICAL_ORIGIN}/{SMOKE_QUERY}"),
)
MAX_SECONDS = 300


def assert_public_config(payload: dict[str, Any]) -> None:
    cognito = payload.get("cognito") or {}
    assert payload.get("auth_mode") == "cognito", "production auth mode changed"
    assert cognito.get("redirect_uri") == f"{CANONICAL_ORIGIN}/auth/callback"
    assert cognito.get("post_logout_redirect_uri") == f"{CANONICAL_ORIGIN}/"


def assert_redirect(source: str, expected: str) -> dict[str, Any]:
    response = requests.get(source, allow_redirects=False, timeout=15)
    location = response.headers.get("Location", "")
    assert response.status_code == 301, f"{source} returned HTTP {response.status_code}"
    assert location == expected, f"{source} redirected to {location!r}"
    return {"source": source, "status": response.status_code, "location": location}


def _launch_browser(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if "Executable doesn't exist" not in str(error):
            raise
        return playwright.chromium.launch(channel="chrome")


def write_failure(
    artifact_dir: Path,
    error: BaseException,
    *,
    page: Any | None,
    observations: dict[str, Any],
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "failure.txt").write_text(
        "".join(traceback.format_exception(error)),
        encoding="utf-8",
    )
    (artifact_dir / "observations.json").write_text(
        json.dumps(observations, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if page is not None:
        try:
            (artifact_dir / "failure-page.txt").write_text(
                page.locator("body").inner_text(),
                encoding="utf-8",
            )
            page.screenshot(
                path=str(artifact_dir / "failure-screenshot.png"),
                full_page=True,
            )
        except Exception:
            pass


def run_live(artifact_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    artifact_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    observations: dict[str, Any] = {"redirects": []}
    page: Any | None = None
    browser: Any | None = None
    try:
        for source, expected in REDIRECT_CASES:
            observations["redirects"].append(assert_redirect(source, expected))

        health = requests.get(f"{CANONICAL_ORIGIN}/api/health", timeout=15)
        readiness = requests.get(f"{CANONICAL_ORIGIN}/api/readiness", timeout=15)
        public_config_response = requests.get(
            f"{CANONICAL_ORIGIN}/api/public-config", timeout=15
        )
        health.raise_for_status()
        readiness.raise_for_status()
        public_config_response.raise_for_status()
        readiness_payload = readiness.json()
        public_config = public_config_response.json()
        assert readiness_payload.get("status") == "ready"
        assert_public_config(public_config)
        observations["health_status"] = health.status_code
        observations["readiness"] = readiness_payload
        observations["public_config"] = {
            "auth_mode": public_config.get("auth_mode"),
            "redirect_uri": (public_config.get("cognito") or {}).get("redirect_uri"),
            "post_logout_redirect_uri": (public_config.get("cognito") or {}).get(
                "post_logout_redirect_uri"
            ),
        }

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.goto(REDIRECT_CASES[1][0], wait_until="domcontentloaded", timeout=30_000)
            assert page.url == REDIRECT_CASES[1][1]
            page.locator("#root").wait_for(state="attached", timeout=15_000)
            page.get_by_role("button", name="Sign in").wait_for(timeout=15_000)
            page.screenshot(path=str(artifact_dir / "canonical-page.png"), full_page=True)
            observations["browser_final_url"] = page.url
            observations["browser_page_text"] = page.locator("body").inner_text()
            page.get_by_role("button", name="Sign in").click()
            page.wait_for_url("**.amazoncognito.com/**", timeout=30_000)
            authorization_url = urlparse(page.url)
            redirect_uri = parse_qs(authorization_url.query).get("redirect_uri", [""])[0]
            assert redirect_uri == f"{CANONICAL_ORIGIN}/auth/callback"
            observations["signin_redirect_uri"] = redirect_uri
            browser.close()
            browser = None

        elapsed = time.monotonic() - started
        assert elapsed <= MAX_SECONDS, f"smoke exceeded {MAX_SECONDS} seconds"
        observations["elapsed_seconds"] = round(elapsed, 3)
        (artifact_dir / "observations.json").write_text(
            json.dumps(observations, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except BaseException as error:
        write_failure(
            artifact_dir,
            error,
            page=page,
            observations=observations,
        )
        raise
    finally:
        if browser is not None:
            browser.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-live-aws", action="store_true")
    parser.add_argument("--artifact-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    if not arguments.allow_live_aws:
        print("error: --allow-live-aws is required", file=sys.stderr)
        return 2
    try:
        run_live(arguments.artifact_dir)
    except BaseException as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print("PASS www-to-apex production browser smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
