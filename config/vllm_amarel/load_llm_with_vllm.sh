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

if [[ ! -f "$profile_path" ]]; then
    echo "Model profile registry not found: $profile_path" >&2
    exit 66
fi
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

try:
    registry = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"Unable to read model profile registry {path}: {error}")
if not isinstance(registry, dict):
    raise SystemExit(f"Model profile registry must be a JSON object: {path}")
profile = registry.get(model_id)
if not isinstance(profile, dict):
    raise SystemExit(f"Model {model_id!r} is not configured in {path}")
settings = profile.get("vllm")
if not isinstance(settings, dict):
    raise SystemExit(f"Model {model_id!r} has no vllm launch configuration")


def positive_int(name: str) -> int:
    value = settings.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"vllm.{name} must be a positive integer")
    return value


def required_bool(name: str) -> bool:
    value = settings.get(name)
    if not isinstance(value, bool):
        raise SystemExit(f"vllm.{name} must be true or false")
    return value


tensor_parallel_size = positive_int("tensor_parallel_size")
max_model_len = positive_int("max_model_len")
gpu_memory_utilization = settings.get("gpu_memory_utilization")
if (
    isinstance(gpu_memory_utilization, bool)
    or not isinstance(gpu_memory_utilization, (int, float))
    or not 0 < gpu_memory_utilization <= 1
):
    raise SystemExit("vllm.gpu_memory_utilization must be greater than 0 and at most 1")
enforce_eager = required_bool("enforce_eager")
enable_auto_tool_choice = required_bool("enable_auto_tool_choice")
tool_call_parser = settings.get("tool_call_parser")
if (
    not isinstance(tool_call_parser, str)
    or not tool_call_parser.strip()
    or "\n" in tool_call_parser
    or "\t" in tool_call_parser
):
    raise SystemExit("vllm.tool_call_parser must be a non-empty single-line string")

chat_template = settings.get("chat_template")
if chat_template is None:
    chat_template = ""
elif (
    not isinstance(chat_template, str)
    or not chat_template.strip()
    or "\n" in chat_template
    or "\r" in chat_template
    or "\t" in chat_template
):
    raise SystemExit("vllm.chat_template must be null or a non-empty single-line path")
else:
    chat_template = chat_template.strip()

dtype = settings.get("dtype", "auto")
allowed_dtypes = {"auto", "bfloat16", "float", "float16", "float32", "half"}
if dtype not in allowed_dtypes:
    raise SystemExit(f"vllm.dtype must be one of: {', '.join(sorted(allowed_dtypes))}")

quantization = settings.get("quantization")
if quantization is None:
    quantization = ""
elif (
    not isinstance(quantization, str)
    or not quantization.strip()
    or "\n" in quantization
    or "\r" in quantization
    or "\t" in quantization
):
    raise SystemExit("vllm.quantization must be null or a non-empty single-line string")
else:
    quantization = quantization.strip()

kv_cache_dtype = settings.get("kv_cache_dtype", "auto")
allowed_kv_cache_dtypes = {
    "auto",
    "bfloat16",
    "float16",
    "fp8",
    "fp8_ds_mla",
    "fp8_e4m3",
    "fp8_e5m2",
    "fp8_inc",
}
if kv_cache_dtype not in allowed_kv_cache_dtypes:
    raise SystemExit(
        "vllm.kv_cache_dtype must be one of: "
        f"{', '.join(sorted(allowed_kv_cache_dtypes))}"
    )

for value in (
    tensor_parallel_size,
    max_model_len,
    gpu_memory_utilization,
    str(enforce_eager).lower(),
    str(enable_auto_tool_choice).lower(),
    tool_call_parser.strip(),
    chat_template,
    dtype,
    quantization,
    kv_cache_dtype,
):
    print(value)
PY
)
mapfile -t launch_settings <<<"$profile_values"
if [[ ${#launch_settings[@]} -ne 10 ]]; then
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

vllm_args=(
    serve "$model_name"
    --tensor-parallel-size "$tensor_parallel_size"
    --max-model-len "$max_model_len"
    --gpu-memory-utilization "$gpu_memory_utilization"
    --dtype "$dtype"
)
if [[ -n "$quantization" ]]; then
    vllm_args+=(--quantization "$quantization")
fi
vllm_args+=(--kv-cache-dtype "$kv_cache_dtype")
if [[ "$enforce_eager" == "true" ]]; then
    vllm_args+=(--enforce-eager)
fi
if [[ "$enable_auto_tool_choice" == "true" ]]; then
    vllm_args+=(--enable-auto-tool-choice)
fi
vllm_args+=(--tool-call-parser "$tool_call_parser")

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
