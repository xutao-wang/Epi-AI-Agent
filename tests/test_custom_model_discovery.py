from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_fastapi import (
    StartupConfigurationError,
    configure_and_verify_providers,
    parse_args,
)
from utils.model_runtime_profiles import CUSTOM_MODELS_PATH_ENV
from utils.model_runtime_profiles import ENABLED_CUSTOM_ENDPOINTS_ENV
from utils.model_runtime_profiles import MODEL_PROFILES_PATH_ENV


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
    from utils.provider_startup import (
        DiscoveredModelMetadata,
        discover_openai_compatible_models,
    )

    class FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(id="Qwen/Qwen3-72B-Instruct"),
                    SimpleNamespace(
                        id="org/epidemiology-model",
                        max_model_len=8192,
                    ),
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
        DiscoveredModelMetadata(
            model_id="Qwen/Qwen3-72B-Instruct",
            max_model_len=None,
        ),
        DiscoveredModelMetadata(
            model_id="org/epidemiology-model",
            max_model_len=8192,
        ),
    )


def test_discovered_model_uses_exact_predefined_profile_and_live_context(
    tmp_path,
) -> None:
    from utils.model_runtime_profiles import (
        CustomEndpointEntry,
        load_compatible_model_profiles,
    )
    from utils.provider_startup import DiscoveredModelMetadata

    profile_path = tmp_path / "model_profiles.json"
    profile_path.write_text(
        json.dumps(
            {
                "google/gemma-4-31B-it": {
                    "label": "Gemma 4 31B Instruct",
                    "initial_output_tokens": 1024,
                    "automatic_output_token_ceiling": 2048,
                    "user_output_token_increment": 1024,
                    "absolute_output_token_ceiling": 4096,
                    "routing_context_char_ceiling": 16000,
                    "supports_vision": True,
                    "supports_mid_conversation_system": True,
                    "supports_sampling_controls": True,
                    "request_timeout_seconds": 240,
                },
                "Qwen/Qwen3-32B": {
                    "label": "Qwen 3 32B",
                    "supports_sampling_controls": True,
                },
            }
        ),
        encoding="utf-8",
    )
    model_profiles = load_compatible_model_profiles(profile_path)
    endpoint = CustomEndpointEntry(
        endpoint_id="vllm",
        base_url="http://127.0.0.1:8000/v1",
        discover_models=True,
    )

    profiles = endpoint.profiles_for(
        [
            DiscoveredModelMetadata(
                model_id="google/gemma-4-31B-it",
                max_model_len=8192,
            ),
            DiscoveredModelMetadata(
                model_id="Qwen/Qwen3-32B",
                max_model_len=32768,
            ),
        ],
        model_profiles=model_profiles,
    )

    gemma = profiles["vllm:google/gemma-4-31B-it"]
    assert gemma.base_label == "vllm:google/Gemma 4 31B Instruct"
    assert gemma.context_window_tokens == 8192
    assert gemma.initial_output_tokens == 1024
    assert gemma.request_timeout_seconds == 240
    assert gemma.supports_vision is True

    qwen = profiles["vllm:Qwen/Qwen3-32B"]
    assert qwen.base_label == "vllm:Qwen/Qwen 3 32B"
    assert qwen.context_window_tokens == 32768
    assert qwen.initial_output_tokens == 1024
    assert qwen.automatic_output_token_ceiling == 2048
    assert qwen.user_output_token_increment == 1024
    assert qwen.absolute_output_token_ceiling == 4096
    assert qwen.request_timeout_seconds == 180
    assert qwen.workflow_timeout_seconds == 600
    assert qwen.supports_vision is False
    assert qwen.supports_mid_conversation_system is False
    assert qwen.summary == "Model discovered from a compatible endpoint."


def test_custom_endpoint_ui_label_keeps_endpoint_and_company_without_reasoning(
) -> None:
    from utils.model_runtime_profiles import (
        CompatibleModelProfileEntry,
        CustomEndpointEntry,
    )

    endpoint = CustomEndpointEntry(
        endpoint_id="vllm",
        label="Local vLLM",
        base_url="http://127.0.0.1:8000/v1",
        discover_models=True,
    )

    profile = endpoint.profiles_for(
        ["google/gemma-4-31B-it"],
        model_profiles={
            "google/gemma-4-31B-it": CompatibleModelProfileEntry(
                label="Gemma 4 31B Instruct"
            )
        },
    )["vllm:google/gemma-4-31B-it"]

    assert profile.descriptor()["label"] == (
        "vllm:google/Gemma 4 31B Instruct"
    )


@pytest.mark.parametrize(
    ("model_id", "expected_label"),
    [
        ("gpt-5.4", "gpt-5.4 (Standard)"),
        ("gpt-5.6-sol", "gpt-5.6-sol (Medium)"),
        ("claude-opus-5", "Claude Opus 5 (Medium)"),
        ("claude-haiku-4-5", "Claude Haiku 4.5 (Standard)"),
    ],
)
def test_builtin_ui_labels_keep_reasoning_level(
    model_id: str,
    expected_label: str,
) -> None:
    from utils.model_runtime_profiles import model_runtime_profile

    assert model_runtime_profile(model_id).descriptor()["label"] == expected_label


def test_startup_loads_profile_registry_for_discovered_model(tmp_path) -> None:
    from utils.provider_startup import DiscoveredModelMetadata

    endpoint_path = tmp_path / "custom_models.json"
    _write_discovery_registry(endpoint_path)
    profile_path = tmp_path / "model_profiles.json"
    profile_path.write_text(
        json.dumps(
            {
                "Qwen/Qwen3-32B": {
                    "supports_vision": True,
                    "request_timeout_seconds": 321,
                }
            }
        ),
        encoding="utf-8",
    )
    environ = {
        CUSTOM_MODELS_PATH_ENV: str(endpoint_path),
        MODEL_PROFILES_PATH_ENV: str(profile_path),
    }

    catalog = configure_and_verify_providers(
        project_root=tmp_path,
        environ=environ,
        input_fn=lambda _prompt: "3",
        discoverer=lambda _key, **_kwargs: (
            DiscoveredModelMetadata(
                model_id="Qwen/Qwen3-32B",
                max_model_len=32768,
            ),
        ),
        verifier=lambda _provider, _key, **_kwargs: None,
        persist=lambda _root, _values: None,
    )

    profile = catalog.registered_profiles["vllm:Qwen/Qwen3-32B"]
    assert profile.supports_vision is True
    assert profile.request_timeout_seconds == 321
    assert profile.context_window_tokens == 32768


def test_shipped_gemma_profile_keeps_summary_optional() -> None:
    from utils.model_runtime_profiles import load_compatible_model_profiles

    gemma = load_compatible_model_profiles()["google/gemma-4-31B-it"]

    assert gemma.summary is None
    assert gemma.supports_vision is True
    assert gemma.request_timeout_seconds == 180
    assert gemma.vllm is not None
    assert gemma.vllm.dtype == "auto"
    assert gemma.vllm.quantization == "fp8"
    assert gemma.vllm.kv_cache_dtype == "auto"


def test_shipped_qwen3_next_fp8_profile_uses_model_supplied_format() -> None:
    from utils.model_runtime_profiles import load_compatible_model_profiles

    qwen = load_compatible_model_profiles()[
        "Qwen/Qwen3-Next-80B-A3B-Instruct-FP8"
    ]

    assert qwen.label == "Qwen3-Next 80B A3B Instruct FP8"
    assert qwen.initial_output_tokens == 2048
    assert qwen.automatic_output_token_ceiling == 4096
    assert qwen.user_output_token_increment == 2048
    assert qwen.absolute_output_token_ceiling == 8192
    assert qwen.supports_vision is False
    assert qwen.supports_mid_conversation_system is False
    assert qwen.vllm is not None
    assert qwen.vllm.tensor_parallel_size == 4
    assert qwen.vllm.max_model_len == 32768
    assert qwen.vllm.quantization is None
    assert qwen.vllm.tool_call_parser == "hermes"
    assert qwen.vllm.chat_template is None


def test_compatible_profile_accepts_valid_vllm_launch_settings() -> None:
    from utils.model_runtime_profiles import CompatibleModelProfileEntry

    profile = CompatibleModelProfileEntry.model_validate(
        {
            "vllm": {
                "tensor_parallel_size": 4,
                "max_model_len": 32768,
                "gpu_memory_utilization": 0.9,
                "enforce_eager": True,
                "enable_auto_tool_choice": True,
                "tool_call_parser": "gemma4",
                "dtype": "auto",
                "quantization": "fp8",
                "kv_cache_dtype": "auto",
            }
        }
    )

    assert profile.vllm is not None
    assert profile.vllm.max_model_len == 32768
    assert profile.vllm.tool_call_parser == "gemma4"
    assert profile.vllm.dtype == "auto"
    assert profile.vllm.quantization == "fp8"
    assert profile.vllm.kv_cache_dtype == "auto"


def test_vllm_launcher_reads_shipped_gemma_profile(tmp_path) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "config/vllm_amarel/load_llm_with_vllm.sh"
    app_root = tmp_path / "apps"
    image = app_root / "singularity_images/vllm-openai_v0.19.1.sif"
    image.parent.mkdir(parents=True)
    image.touch()
    captured = tmp_path / "apptainer-arguments.txt"
    fake_apptainer = tmp_path / "apptainer"
    fake_apptainer.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n",
        encoding="utf-8",
    )
    fake_apptainer.chmod(0o755)
    environ = {
        **os.environ,
        "APPTAINER_BIN": str(fake_apptainer),
        "VLLM_APP_ROOT": str(app_root),
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
    }

    result = subprocess.run(
        [
            "bash",
            str(launcher),
            image.name,
            "google/gemma-4-31B-it",
        ],
        cwd=project_root,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert arguments[-19:] == [
        "vllm",
        "serve",
        "google/gemma-4-31B-it",
        "--tensor-parallel-size",
        "4",
        "--max-model-len",
        "32768",
        "--gpu-memory-utilization",
        "0.9",
        "--dtype",
        "auto",
        "--quantization",
        "fp8",
        "--kv-cache-dtype",
        "auto",
        "--enforce-eager",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "gemma4",
    ]


def test_vllm_launcher_uses_qwen_model_supplied_chat_template(tmp_path) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "config/vllm_amarel/load_llm_with_vllm.sh"
    app_root = tmp_path / "apps"
    image = app_root / "singularity_images/vllm-openai_v0.19.1.sif"
    image.parent.mkdir(parents=True)
    image.touch()
    captured = tmp_path / "apptainer-arguments.txt"
    fake_apptainer = tmp_path / "apptainer"
    fake_apptainer.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n",
        encoding="utf-8",
    )
    fake_apptainer.chmod(0o755)
    environ = {
        **os.environ,
        "APPTAINER_BIN": str(fake_apptainer),
        "VLLM_APP_ROOT": str(app_root),
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
    }

    result = subprocess.run(
        [
            "bash",
            str(launcher),
            image.name,
            "Qwen/Qwen3-Next-80B-A3B-Instruct-FP8",
        ],
        cwd=project_root,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert "--chat-template" not in arguments
    assert "/vllm_chat_template.jinja" not in arguments
    assert arguments[-16:] == [
        "vllm",
        "serve",
        "Qwen/Qwen3-Next-80B-A3B-Instruct-FP8",
        "--tensor-parallel-size",
        "4",
        "--max-model-len",
        "32768",
        "--gpu-memory-utilization",
        "0.9",
        "--dtype",
        "auto",
        "--kv-cache-dtype",
        "auto",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
    ]


def test_safe_token_defaults_shrink_to_live_context_window() -> None:
    from utils.model_runtime_profiles import CompatibleModelProfileEntry

    assert CompatibleModelProfileEntry().token_settings(4096) == (
        512,
        1024,
        512,
        2048,
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


def _incomplete_model_answer(response_id: str, output_tokens: int):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="continued output",
        id=response_id,
        response_metadata={
            "finish_reason": "length",
            "id": response_id,
            "model_name": "local-model",
        },
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": output_tokens,
            "total_tokens": 100 + output_tokens,
        },
    )


def _incomplete_output_patch(profile, *, chain_output_tokens: int):
    from epi_agent.runtime import _model_answer_patch

    output_state = {
        "phase": "automatic",
        "chain_response_ids": ["segment-1"],
        "chain_output_tokens": chain_output_tokens,
    }
    return _model_answer_patch(
        {"messages": []},
        agent_config=SimpleNamespace(model_profile=profile),
        answer=_incomplete_model_answer("segment-2", 2_048),
        duration_ms=10,
        iteration_count=1,
        output_state=output_state,
        phase="automatic",
    )


def _local_output_profile():
    from utils.model_runtime_profiles import (
        CompatibleModelProfileEntry,
        CustomEndpointEntry,
    )

    endpoint = CustomEndpointEntry(
        endpoint_id="vllm",
        base_url="http://127.0.0.1:8000/v1",
        discover_models=True,
    )
    configured = CompatibleModelProfileEntry(
        initial_output_tokens=2_048,
        automatic_output_token_ceiling=4_096,
        user_output_token_increment=2_048,
        absolute_output_token_ceiling=8_192,
    )
    return endpoint.profiles_for(
        ["Qwen/Qwen3-Next-80B-A3B-Instruct-FP8"],
        model_profiles={
            "Qwen/Qwen3-Next-80B-A3B-Instruct-FP8": configured,
        },
    )["vllm:Qwen/Qwen3-Next-80B-A3B-Instruct-FP8"]


def test_local_compatible_model_continues_without_user_approval() -> None:
    patch = _incomplete_output_patch(
        _local_output_profile(),
        chain_output_tokens=2_048,
    )

    assert patch["model_output_state"]["phase"] == "automatic"
    assert patch["model_output_state"]["chain_output_tokens"] == 4_096
    assert patch["completion_blocked"] is True
    assert "terminal_error" not in patch


def test_local_compatible_model_stops_at_technical_ceiling() -> None:
    patch = _incomplete_output_patch(
        _local_output_profile(),
        chain_output_tokens=6_144,
    )

    assert patch["model_output_state"]["phase"] == "exhausted"
    assert patch["terminal_error"]["code"] == "MODEL_OUTPUT_LIMIT_EXHAUSTED"
    assert patch["completion_blocked"] is False


def test_paid_model_still_waits_for_user_approval() -> None:
    from utils.model_runtime_profiles import MODEL_RUNTIME_PROFILES

    patch = _incomplete_output_patch(
        MODEL_RUNTIME_PROFILES["gpt-5.6-sol"],
        chain_output_tokens=25_000,
    )

    assert patch["model_output_state"]["phase"] == "awaiting_user"
    assert patch["completion_blocked"] is True
    assert "terminal_error" not in patch


def test_local_continuation_budget_cannot_overshoot_technical_ceiling() -> None:
    from epi_agent.runtime import _prepare_model_request

    profile = _local_output_profile()
    prepared = _prepare_model_request(
        {
            "failure_signatures": [],
            "iteration_count": 1,
            "messages": [],
            "model_output_state": {
                "phase": "automatic",
                "chain_output_tokens": 7_000,
            },
        },
        agent_config=SimpleNamespace(
            context_prompt_factory=lambda _state: "",
            max_iterations=20,
            model_profile=profile,
            system_prompt="test system prompt",
        ),
    )

    assert not isinstance(prepared, dict)
    assert prepared[-1] == 1_192
