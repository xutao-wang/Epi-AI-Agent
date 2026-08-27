from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from run_fastapi import (
    StartupConfigurationError,
    configure_and_verify_providers,
    parse_args,
)
from utils.model_runtime_profiles import CUSTOM_MODELS_PATH_ENV
from utils.model_runtime_profiles import ENABLED_CUSTOM_ENDPOINTS_ENV


def _write_discovery_registry(path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "endpoint_id": "vllm",
                    "label": "Local vLLM",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "discover_models": True,
                    "api_key_env": "",
                }
            ]
        ),
        encoding="utf-8",
    )


def _write_fixed_registry(path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "id": "vllm:qwen3",
                    "label": "Fixed local model",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "model": "Qwen/Qwen3-72B-Instruct",
                    "api_key_env": "",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_unselected_discovery_endpoint_is_not_contacted(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_discovery_registry(registry)
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(registry),
        "OPENAI_API_KEY": "test-openai-key",
    }

    def unexpected_discovery(*_args, **_kwargs):
        raise AssertionError("unselected local endpoint was contacted")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("provider menu unexpectedly opened")
        ),
        verifier=lambda provider, _key, **_kwargs: None,
        discoverer=unexpected_discovery,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == (
        "gpt-5.4",
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    )


def test_unselected_fixed_custom_endpoint_is_not_contacted(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_fixed_registry(registry)
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(registry),
        "OPENAI_API_KEY": "test-openai-key",
    }

    def verify_builtin_only(provider, _key, **_kwargs) -> None:
        if provider == "openai_compatible":
            raise AssertionError("unselected fixed endpoint was contacted")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        verifier=verify_builtin_only,
        persist=lambda _root, _values: None,
    )

    assert "vllm:qwen3" not in catalog.available_model_ids


def test_selecting_discovery_endpoint_registers_every_served_model(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_discovery_registry(registry)
    environ = {CUSTOM_MODELS_PATH_ENV: str(registry)}
    selections = iter(["3"])
    discovery_calls = 0

    def discover(_key, **_kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        return (
            "Qwen/Qwen3-72B-Instruct",
            "org/epidemiology-model",
        )

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(selections),
        discoverer=discover,
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == (
        "vllm:Qwen/Qwen3-72B-Instruct",
        "vllm:org/epidemiology-model",
    )
    assert (
        catalog.registered_profiles["vllm:Qwen/Qwen3-72B-Instruct"].served_model_id
        == "Qwen/Qwen3-72B-Instruct"
    )
    assert discovery_calls == 1


def test_openai_compatible_discovery_returns_exact_served_ids() -> None:
    from utils.provider_startup import discover_openai_compatible_models

    class FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="Qwen/Qwen3-72B-Instruct"),
                    SimpleNamespace(id="org/epidemiology-model"),
                ]
            )

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.models = FakeModels()

    assert discover_openai_compatible_models(
        "",
        base_url="http://127.0.0.1:8000/v1",
        client_factory=FakeClient,
    ) == (
        "Qwen/Qwen3-72B-Instruct",
        "org/epidemiology-model",
    )


def test_discovered_profile_routes_inference_to_exact_remote_model(monkeypatch) -> None:
    import llm_vllm
    from utils.model_runtime_profiles import CustomEndpointEntry

    endpoint = CustomEndpointEntry(
        endpoint_id="vllm",
        label="Local vLLM",
        base_url="http://127.0.0.1:8000/v1",
        discover_models=True,
    )
    profile = endpoint.profiles_for(["Qwen/Qwen3-72B-Instruct"])[
        "vllm:Qwen/Qwen3-72B-Instruct"
    ]
    monkeypatch.setattr(llm_vllm, "ChatOpenAI", lambda **kwargs: kwargs)

    client_options = llm_vllm.build_chat_llm(
        model_name=profile.model_id,
        profile=profile,
    )

    assert client_options["model"] == "Qwen/Qwen3-72B-Instruct"
    assert client_options["base_url"] == "http://127.0.0.1:8000/v1"


def test_native_app_default_port_does_not_conflict_with_vllm() -> None:
    assert parse_args([]).port == 8080


def test_discovered_model_cannot_replace_existing_custom_model(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "vllm:Qwen/Qwen3-72B-Instruct",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "model": "fixed-model",
                },
                {
                    "endpoint_id": "vllm",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "discover_models": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    selections = iter(["3", "2"])

    with pytest.raises(StartupConfigurationError, match="duplicates"):
        configure_and_verify_providers(
            project_root=tmp_path,
            environ={CUSTOM_MODELS_PATH_ENV: str(registry)},
            input_fn=lambda _prompt: next(selections),
            discoverer=lambda _key, **_kwargs: (
                "Qwen/Qwen3-72B-Instruct",
            ),
            verifier=lambda _provider, _key, **_kwargs: None,
            persist=lambda _root, _values: None,
        )


def test_persisted_local_selection_refreshes_without_opening_menu(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_discovery_registry(registry)
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(registry),
        ENABLED_CUSTOM_ENDPOINTS_ENV: "vllm",
    }

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: (_ for _ in ()).throw(
            AssertionError("persisted selection unexpectedly reopened the menu")
        ),
        discoverer=lambda _key, **_kwargs: ("served-model",),
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == ("vllm:served-model",)


def test_selected_legacy_fixed_model_remains_supported(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_fixed_registry(registry)
    environ = {CUSTOM_MODELS_PATH_ENV: str(registry)}
    selections = iter(["3"])

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(selections),
        discoverer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fixed model unexpectedly used discovery")
        ),
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == ("vllm:qwen3",)
    assert catalog.registered_profiles["vllm:qwen3"].served_model_id == (
        "Qwen/Qwen3-72B-Instruct"
    )


def test_empty_or_duplicate_discovery_results_are_rejected() -> None:
    from utils.model_runtime_profiles import CustomEndpointEntry

    endpoint = CustomEndpointEntry(
        endpoint_id="vllm",
        base_url="http://127.0.0.1:8000/v1",
        discover_models=True,
    )

    with pytest.raises(ValueError, match="returned no models"):
        endpoint.profiles_for([])
    with pytest.raises(ValueError, match="returned duplicate model id"):
        endpoint.profiles_for(["same-model", "same-model"])


def test_unreachable_selected_endpoint_does_not_hide_usable_builtin_models(
    tmp_path,
) -> None:
    from utils.provider_startup import ProviderCredentialError

    registry = tmp_path / "custom_models.json"
    _write_discovery_registry(registry)
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(registry),
        ENABLED_CUSTOM_ENDPOINTS_ENV: "vllm",
        "OPENAI_API_KEY": "test-openai-key",
    }

    def unavailable(*_args, **_kwargs):
        raise ProviderCredentialError("network", "endpoint unavailable")

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        discoverer=unavailable,
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
    )

    assert all(
        not model_id.startswith("vllm:")
        for model_id in catalog.available_model_ids
    )


def test_api_app_import_does_not_build_provider_catalog(monkeypatch, tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    _write_discovery_registry(registry)
    monkeypatch.setenv(CUSTOM_MODELS_PATH_ENV, str(registry))
    monkeypatch.setenv(ENABLED_CUSTOM_ENDPOINTS_ENV, "vllm")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sys.modules.pop("api.app", None)

    module = importlib.import_module("api.app")

    assert callable(module.build_application)


def test_user_selects_one_endpoint_from_multi_endpoint_registry(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "endpoint_id": "vllm-a",
                    "base_url": "http://127.0.0.1:8001/v1",
                    "discover_models": True,
                },
                {
                    "endpoint_id": "vllm-b",
                    "base_url": "http://127.0.0.1:8002/v1",
                    "discover_models": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    environ = {CUSTOM_MODELS_PATH_ENV: str(registry)}
    selections = iter(["3", "2"])
    contacted_urls = []

    def discover(_key, *, base_url):
        contacted_urls.append(base_url)
        return ("served-model",)

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(selections),
        discoverer=discover,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == ("vllm-b:served-model",)
    assert contacted_urls == ["http://127.0.0.1:8002/v1"]
    assert environ[ENABLED_CUSTOM_ENDPOINTS_ENV] == "vllm-b"


def test_endpoint_menu_rejects_zero_selection(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "endpoint_id": "vllm-a",
                    "base_url": "http://127.0.0.1:8001/v1",
                    "discover_models": True,
                },
                {
                    "endpoint_id": "vllm-b",
                    "base_url": "http://127.0.0.1:8002/v1",
                    "discover_models": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    environ = {CUSTOM_MODELS_PATH_ENV: str(registry)}
    selections = iter(["3", "0", "3", "1"])
    contacted_urls = []

    def discover(_key, *, base_url):
        contacted_urls.append(base_url)
        return ("served-model",)

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: next(selections),
        discoverer=discover,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == ("vllm-a:served-model",)
    assert contacted_urls == ["http://127.0.0.1:8001/v1"]
    assert environ[ENABLED_CUSTOM_ENDPOINTS_ENV] == "vllm-a"


def test_discovery_and_fixed_model_share_one_endpoint_request(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "vllm:fixed-model",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "model": "fixed-model",
                },
                {
                    "endpoint_id": "vllm",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "discover_models": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(registry),
        ENABLED_CUSTOM_ENDPOINTS_ENV: "vllm,vllm:fixed-model",
    }
    endpoint_requests = 0

    def discover(_key, **_kwargs):
        nonlocal endpoint_requests
        endpoint_requests += 1
        return ("discovered-model",)

    def verify(provider, _key, **_kwargs) -> None:
        nonlocal endpoint_requests
        if provider == "openai_compatible":
            endpoint_requests += 1

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        discoverer=discover,
        verifier=verify,
        persist=lambda _root, _values: None,
    )

    assert catalog.available_model_ids == (
        "vllm:fixed-model",
        "vllm:discovered-model",
    )
    assert endpoint_requests == 1


def test_fixed_model_id_cannot_conflict_with_discovery_endpoint_id(tmp_path) -> None:
    registry = tmp_path / "custom_models.json"
    registry.write_text(
        json.dumps(
            [
                {
                    "id": "vllm",
                    "base_url": "http://127.0.0.1:8001/v1",
                    "model": "fixed-model",
                },
                {
                    "endpoint_id": "vllm",
                    "base_url": "http://127.0.0.1:8002/v1",
                    "discover_models": True,
                },
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="conflicts"):
        configure_and_verify_providers(
            project_root=tmp_path,
            environ={CUSTOM_MODELS_PATH_ENV: str(registry)},
            input_fn=lambda _prompt: "3",
            discoverer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("conflicting endpoint was contacted")
            ),
            persist=lambda _root, _values: None,
        )
