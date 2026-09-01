#!/usr/bin/env bash
set -euo pipefail

# Launch one native vLLM multiprocessing rank per allocated Slurm node.
# Run this script only after obtaining a multi-node salloc/srun allocation.

usage() {
    cat >&2 <<EOF
Usage: $0 IMAGE_NAME MODEL_NAME NODE_COUNT

Optional environment variables:
  VLLM_MASTER_PORT       Distributed rendezvous port (default: 29501)
  VLLM_NETWORK_IFACE     Interface for both NCCL and Gloo (for example: ib0)
  VLLM_CPUS_PER_TASK     CPUs assigned to each node task
EOF
}

die() {
    local exit_code=$1
    shift
    echo "Error: $*" >&2
    exit "$exit_code"
}

is_positive_integer() {
    [[ $1 =~ ^[1-9][0-9]*$ ]]
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script_path="${script_dir}/$(basename -- "${BASH_SOURCE[0]}")"
project_root=$(cd -- "${script_dir}/../.." && pwd)
single_node_launcher=${VLLM_SINGLE_NODE_LAUNCHER:-${script_dir}/load_llm_with_vllm.sh}

run_node_task() {
    if [[ $# -ne 6 ]]; then
        die 64 "internal node task requires IMAGE MODEL NODE_COUNT HEAD PORT TP"
    fi

    local image_name=$1
    local model_name=$2
    local node_count=$3
    local head_node=$4
    local master_port=$5
    local tensor_parallel_size=$6
    local rank=${SLURM_PROCID:-}

    is_positive_integer "$node_count" || die 64 "NODE_COUNT must be a positive integer"
    is_positive_integer "$tensor_parallel_size" || \
        die 65 "tensor parallel size must be a positive integer"
    [[ $master_port =~ ^[0-9]+$ ]] && \
        (( master_port >= 1024 && master_port <= 65535 )) || \
        die 64 "VLLM_MASTER_PORT must be between 1024 and 65535"
    [[ $rank =~ ^[0-9]+$ ]] || die 69 "SLURM_PROCID is unavailable for this node task"
    (( rank < node_count )) || \
        die 69 "Slurm rank ${rank} is outside NODE_COUNT=${node_count}"
    [[ -x $single_node_launcher ]] || \
        die 69 "single-node launcher is not executable: ${single_node_launcher}"

    local visible_devices=${CUDA_VISIBLE_DEVICES:-}
    [[ -n $visible_devices ]] || \
        die 69 "CUDA_VISIBLE_DEVICES is empty on rank ${rank}"
    local -a visible_gpu_ids=()
    IFS=',' read -r -a visible_gpu_ids <<<"$visible_devices"
    (( ${#visible_gpu_ids[@]} >= tensor_parallel_size )) || \
        die 69 "rank ${rank} sees ${#visible_gpu_ids[@]} GPUs; model requires ${tensor_parallel_size}"

    if [[ -n ${VLLM_NETWORK_IFACE:-} ]]; then
        [[ $VLLM_NETWORK_IFACE =~ ^[A-Za-z0-9_.:,=^-]+$ ]] || \
            die 64 "VLLM_NETWORK_IFACE contains unsupported characters"
        export NCCL_SOCKET_IFNAME=$VLLM_NETWORK_IFACE
        export GLOO_SOCKET_IFNAME=$VLLM_NETWORK_IFACE
    fi

    local -a distributed_args=(
        --distributed-executor-backend mp
        --nnodes "$node_count"
        --node-rank "$rank"
        --master-addr "$head_node"
        --master-port "$master_port"
        --pipeline-parallel-size "$node_count"
    )
    if (( rank != 0 )); then
        distributed_args+=(--headless)
    fi

    echo "Starting vLLM node rank ${rank}/${node_count} on $(hostname) with GPUs ${visible_devices}"
    exec "$single_node_launcher" \
        "$image_name" "$model_name" "${distributed_args[@]}"
}

if [[ ${1:-} == "--node-task" ]]; then
    shift
    run_node_task "$@"
fi

if [[ $# -ne 3 ]]; then
    usage
    exit 64
fi

image_name=$1
model_name=$2
node_count=$3

is_positive_integer "$node_count" || die 64 "NODE_COUNT must be a positive integer"
[[ -n ${SLURM_JOB_ID:-} ]] || \
    die 69 "no Slurm allocation detected; run this after salloc or interactive srun"
[[ -n ${SLURM_JOB_NODELIST:-} ]] || die 69 "SLURM_JOB_NODELIST is unavailable"

scontrol_bin=${SCONTROL_BIN:-scontrol}
srun_bin=${SRUN_BIN:-srun}
python_bin=${PYTHON_BIN:-python3}
profile_path=${REPORT_AGENT_MODEL_PROFILES_PATH:-${project_root}/config/model_profiles.json}
master_port=${VLLM_MASTER_PORT:-29501}
cpus_per_task=${VLLM_CPUS_PER_TASK:-${SLURM_CPUS_PER_TASK:-1}}

command -v "$scontrol_bin" >/dev/null 2>&1 || \
    die 69 "scontrol executable not found: ${scontrol_bin}"
command -v "$srun_bin" >/dev/null 2>&1 || \
    die 69 "srun executable not found: ${srun_bin}"
command -v "$python_bin" >/dev/null 2>&1 || \
    die 69 "Python executable not found: ${python_bin}"
[[ -f $profile_path ]] || die 66 "model profile registry not found: ${profile_path}"
[[ -x $single_node_launcher ]] || \
    die 69 "single-node launcher is not executable: ${single_node_launcher}"
[[ $master_port =~ ^[0-9]+$ ]] && \
    (( master_port >= 1024 && master_port <= 65535 )) || \
    die 64 "VLLM_MASTER_PORT must be between 1024 and 65535"
is_positive_integer "$cpus_per_task" || \
    die 64 "VLLM_CPUS_PER_TASK must be a positive integer"
if [[ -n ${VLLM_NETWORK_IFACE:-} ]]; then
    [[ $VLLM_NETWORK_IFACE =~ ^[A-Za-z0-9_.:,=^-]+$ ]] || \
        die 64 "VLLM_NETWORK_IFACE contains unsupported characters"
    export NCCL_SOCKET_IFNAME=$VLLM_NETWORK_IFACE
    export GLOO_SOCKET_IFNAME=$VLLM_NETWORK_IFACE
fi

node_output=$(
    "$scontrol_bin" show hostnames "$SLURM_JOB_NODELIST"
) || die 69 "unable to expand SLURM_JOB_NODELIST=${SLURM_JOB_NODELIST}"
allocated_nodes=()
while IFS= read -r node; do
    [[ -n $node ]] && allocated_nodes+=("$node")
done <<<"$node_output"
actual_node_count=${#allocated_nodes[@]}
(( actual_node_count > 0 )) || die 69 "the Slurm allocation contains no nodes"
if (( actual_node_count != node_count )); then
    die 64 "requested ${node_count} nodes, but the allocation contains ${actual_node_count}"
fi
head_node=${allocated_nodes[0]}

tensor_parallel_size=$(
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
profile = registry.get(model_id) if isinstance(registry, dict) else None
settings = profile.get("vllm") if isinstance(profile, dict) else None
value = settings.get("tensor_parallel_size") if isinstance(settings, dict) else None
if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
    raise SystemExit(
        f"Model {model_id!r} needs a positive vllm.tensor_parallel_size in {path}"
    )
print(value)
PY
) || die 65 "unable to load the tensor-parallel setting for ${model_name}"

srun_args=(
    "--nodes=${node_count}"
    "--ntasks=${node_count}"
    --ntasks-per-node=1
    "--cpus-per-task=${cpus_per_task}"
    "--gpus-per-task=${tensor_parallel_size}"
    --kill-on-bad-exit=1
    --label
)
if [[ -n ${SLURM_STEP_ID:-} ]]; then
    srun_args+=(--overlap)
fi

echo "Launching ${model_name} across ${node_count} nodes (${tensor_parallel_size} GPUs per node)."
echo "Head node: ${head_node}"
echo "API after startup: http://${head_node}:8000/v1"

exec "$srun_bin" "${srun_args[@]}" \
    "$script_path" --node-task \
    "$image_name" "$model_name" "$node_count" "$head_node" \
    "$master_port" "$tensor_parallel_size"
