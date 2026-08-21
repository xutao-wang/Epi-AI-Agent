from pathlib import Path


ROOT = Path(__file__).parents[1]
FORBIDDEN_PATHS = (
    ".dockerignore",
    "Dockerfile",
    "compose.yaml",
    "infra/aws",
    "deploy/aws",
    "docs/aws",
    "frontend/dist/build-manifest.json",
)


def test_local_only_branch_has_no_deployment_artifacts() -> None:
    assert [path for path in FORBIDDEN_PATHS if (ROOT / path).exists()] == []


def test_active_sources_do_not_reference_removed_hosted_features() -> None:
    roots = (ROOT / "api", ROOT / "frontend" / "src", ROOT / "scripts")
    forbidden = (
        "CognitoTokenVerifier",
        "REPORT_AGENT_AUTH_MODE",
        "REPORT_AGENT_AWS_",
        "REPORT_AGENT_COGNITO_",
        "REPORT_AGENT_PYTHON_WORKER_LAUNCHER",
        "/api/session/provider-key",
    )
    matches: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}:
                source = path.read_text(encoding="utf-8")
                matches.extend(
                    f"{path.relative_to(ROOT)}: {token}"
                    for token in forbidden
                    if token in source
                )
    assert matches == []
