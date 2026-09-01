#!/usr/bin/env bash
set -euo pipefail

# This script is intend to use customized LLM with vllm on the amarel
# Example: refer to load_llm_amarel_instruction.md

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 IMAGE_NAME MODEL_NAME [VLLM_ARG ...]" >&2
    exit 64
fi

image_name=$1
model_name=$2
shift 2
additional_vllm_args=("$@")

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "${script_dir}/../.." && pwd)
profile_path=${REPORT_AGENT_MODEL_PROFILES_PATH:-${project_root}/config/model_profiles.json}
python_bin=${PYTHON_BIN:-python3}
app_root=${VLLM_APP_ROOT:-/projects/f_wj183_1/apps}
apptainer_bin=${APPTAINER_BIN:-/usr/bin/apptainer}
image_path="${app_root}/singularity_images/${image_name}"

if [[ ! -f "$image_path" ]]; then
    echo "Container image not found: $image_path" >&2
    exit 66
fi
if [[ ! -x "$apptainer_bin" ]]; then
    echo "Apptainer executable not found: $apptainer_bin" >&2
    exit 69
fi

profile_values=$(
    "$python_bin" - "$profile_path" "$model_name" <<'PY'
import json
import sys
from pathlib import Path


path = Path(sys.argv[1])
model_id = sys.argv[2]

if path.is_file():
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read model profile registry {path}: {error}")
else:
    registry = {}
if not isinstance(registry, dict):
    raise SystemExit(f"Model profile registry must be a JSON object: {path}")
profile = registry.get(model_id, {})
if not isinstance(profile, dict):
    raise SystemExit(f"Model profile {model_id!r} must be a JSON object")
settings = profile.get("vllm")
if settings is None:
    settings = {}
elif not isinstance(settings, dict):
    raise SystemExit(f"Model {model_id!r} vllm settings must be a JSON object")

allowed_settings = {
    "tensor_parallel_size",
    "max_model_len",
    "gpu_memory_utilization",
    "enforce_eager",
    "enable_auto_tool_choice",
    "tool_call_parser",
    "chat_template",
    "dtype",
    "quantization",
    "kv_cache_dtype",
}
unknown_settings = sorted(set(settings) - allowed_settings)
if unknown_settings:
    raise SystemExit(
        f"Model {model_id!r} has unknown vllm settings: "
        + ", ".join(unknown_settings)
    )


def optional_positive_int(name: str) -> int | None:
    value = settings.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"vllm.{name} must be a positive integer")
    return value


def optional_bool(name: str) -> bool | None:
    value = settings.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SystemExit(f"vllm.{name} must be true or false")
    return value


def optional_single_line(name: str) -> str | None:
    value = settings.get(name)
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\n" in value
        or "\r" in value
        or "\t" in value
    ):
        raise SystemExit(f"vllm.{name} must be a non-empty single-line string")
    return value.strip()


def optional_choice(name: str, allowed: set[str]) -> str | None:
    value = settings.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise SystemExit(f"vllm.{name} must be one of: {', '.join(sorted(allowed))}")
    return value


tensor_parallel_size = optional_positive_int("tensor_parallel_size")
max_model_len = optional_positive_int("max_model_len")
gpu_memory_utilization = settings.get("gpu_memory_utilization")
if gpu_memory_utilization is not None and (
    isinstance(gpu_memory_utilization, bool)
    or not isinstance(gpu_memory_utilization, (int, float))
    or not 0 < gpu_memory_utilization <= 1
):
    raise SystemExit("vllm.gpu_memory_utilization must be greater than 0 and at most 1")
enforce_eager = optional_bool("enforce_eager")
enable_auto_tool_choice = optional_bool("enable_auto_tool_choice")
tool_call_parser = optional_single_line("tool_call_parser")
chat_template = optional_single_line("chat_template")
dtype = optional_choice(
    "dtype",
    {"auto", "bfloat16", "float", "float16", "float32", "half"},
)
quantization = optional_single_line("quantization")
kv_cache_dtype = optional_choice(
    "kv_cache_dtype",
    {
        "auto",
        "bfloat16",
        "float16",
        "fp8",
        "fp8_ds_mla",
        "fp8_e4m3",
        "fp8_e5m2",
        "fp8_inc",
    },
)


def render(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


for value in (
    tensor_parallel_size,
    max_model_len,
    gpu_memory_utilization,
    enforce_eager,
    enable_auto_tool_choice,
    tool_call_parser,
    chat_template,
    dtype,
    quantization,
    kv_cache_dtype,
):
    print(render(value))
print("__VLLM_PROFILE_END__")
PY
)
mapfile -t launch_settings <<<"$profile_values"
if [[ ${#launch_settings[@]} -ne 11 || ${launch_settings[10]} != "__VLLM_PROFILE_END__" ]]; then
    echo "Model '$model_name' returned an incomplete vLLM launch configuration." >&2
    exit 65
fi

tensor_parallel_size=${launch_settings[0]}
max_model_len=${launch_settings[1]}
gpu_memory_utilization=${launch_settings[2]}
enforce_eager=${launch_settings[3]}
enable_auto_tool_choice=${launch_settings[4]}
tool_call_parser=${launch_settings[5]}
chat_template=${launch_settings[6]}
dtype=${launch_settings[7]}
quantization=${launch_settings[8]}
kv_cache_dtype=${launch_settings[9]}

vllm_args=(serve "$model_name")
if [[ -n "$tensor_parallel_size" ]]; then
    vllm_args+=(--tensor-parallel-size "$tensor_parallel_size")
fi
if [[ -n "$max_model_len" ]]; then
    vllm_args+=(--max-model-len "$max_model_len")
fi
if [[ -n "$gpu_memory_utilization" ]]; then
    vllm_args+=(--gpu-memory-utilization "$gpu_memory_utilization")
fi
if [[ -n "$dtype" ]]; then
    vllm_args+=(--dtype "$dtype")
fi
if [[ -n "$quantization" ]]; then
    vllm_args+=(--quantization "$quantization")
fi
if [[ -n "$kv_cache_dtype" ]]; then
    vllm_args+=(--kv-cache-dtype "$kv_cache_dtype")
fi
if [[ "$enforce_eager" == "true" ]]; then
    vllm_args+=(--enforce-eager)
fi
if [[ "$enable_auto_tool_choice" == "true" ]]; then
    vllm_args+=(--enable-auto-tool-choice)
fi
if [[ -n "$tool_call_parser" ]]; then
    vllm_args+=(--tool-call-parser "$tool_call_parser")
fi

template_bind_args=()
if [[ -n "$chat_template" ]]; then
    if [[ "$chat_template" = /* ]]; then
        chat_template_path=$chat_template
    else
        chat_template_path="${project_root}/${chat_template}"
    fi
    if [[ ! -f "$chat_template_path" ]]; then
        echo "Chat template not found: $chat_template_path" >&2
        exit 66
    fi
    container_chat_template=/vllm_chat_template.jinja
    template_bind_args=(
        --bind "${chat_template_path}:${container_chat_template}:ro"
    )
    vllm_args+=(--chat-template "$container_chat_template")
fi
vllm_args+=("${additional_vllm_args[@]}")

container_env_args=(--env "HF_HOME=/hf_cache")
for env_name in \
    CUDA_VISIBLE_DEVICES \
    NCCL_SOCKET_IFNAME GLOO_SOCKET_IFNAME NCCL_DEBUG \
    NCCL_IB_DISABLE NCCL_P2P_DISABLE NCCL_ASYNC_ERROR_HANDLING \
    TORCH_NCCL_ASYNC_ERROR_HANDLING
do
    if [[ -v "$env_name" ]]; then
        container_env_args+=(--env "${env_name}=${!env_name}")
    fi
done

unset \
    SINGULARITY_BINDPATH SINGULARITY_BIND \
    APPTAINER_BINDPATH APPTAINER_BIND

exec "$apptainer_bin" exec \
    --cleanenv \
    --nv \
    "${container_env_args[@]}" \
    --bind "${app_root}/hf_cache:/hf_cache" \
    --bind "${app_root}/vllm_addons:/ext" \
    "${template_bind_args[@]}" \
    "$image_path" \
    vllm "${vllm_args[@]}"
