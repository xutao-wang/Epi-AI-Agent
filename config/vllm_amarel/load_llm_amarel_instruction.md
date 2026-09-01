# Running vLLM on Amarel

Run these launchers only after Slurm has allocated GPU resources. The examples
assume four GPUs and 240 GB of RAM per node in the lab partition.

## Single node

Request an interactive GPU node:

```bash
srun \
  --partition=p_wj183_1 \
  --nodes=1 \
  --job-name=LLM-test \
  --ntasks=1 \
  --cpus-per-task=32 \
  --gres=gpu:4 \
  --mem=240G \
  --time=12:00:00 \
  --pty bash
```

From the project root, launch the configured model:

```bash
image_name=vllm-openai_v0.19.1.sif
# model_name=Qwen/Qwen3-Next-80B-A3B-Instruct-FP8 # need two nodes for initialization
model_name=google/gemma-4-31B-it 
bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name"
```

## Multiple nodes

For multiple nodes, `salloc` is preferred because the launcher creates the
one-task-per-node `srun` step itself. Set `--nodes`, `--ntasks`, and the
launcher `node_count` to the same value:

```bash
node_count=2
salloc \
  --partition=p_wj183_1 \
  --nodes="$node_count" \
  --ntasks="$node_count" \
  --ntasks-per-node=1 \
  --cpus-per-task=32 \
  --gres=gpu:4 \
  --mem=240G \
  --time=2:00:00 \
  --job-name=epi-agent-multinode
```

After the allocation starts, run:

```bash
image_name=vllm-openai_v0.19.1.sif
model_name=Qwen/Qwen3-Next-80B-A3B-Instruct-FP8
node_count=2

bash config/vllm_amarel/load_llm_with_vllm_multi_node.sh \
  "$image_name" "$model_name" "$node_count"
```

The launcher checks that `node_count` exactly matches the allocation, starts
one native vLLM multiprocessing rank per node, and prints the head node and API
URL. It uses the model profile's tensor parallel size within each node and uses
the node count as the pipeline parallel size. Therefore, the same
`config/model_profiles.json` entry is used for single- and multi-node launches.

If the launcher is called from an existing interactive `srun --pty` step, it
automatically adds `srun --overlap`. A plain `salloc` allocation is less prone
to nested-step resource conflicts.

By default, NCCL selects the network transport. Only override it while
diagnosing a transport problem:

```bash
VLLM_NETWORK_IFACE=ib0 NCCL_DEBUG=INFO \
  bash config/vllm_amarel/load_llm_with_vllm_multi_node.sh \
  "$image_name" "$model_name" "$node_count"
```

To avoid a rendezvous-port collision, select another unprivileged port:

```bash
VLLM_MASTER_PORT=29502 \
  bash config/vllm_amarel/load_llm_with_vllm_multi_node.sh \
  "$image_name" "$model_name" "$node_count"
```

Do not set `NCCL_IB_DISABLE=1` or `NCCL_P2P_DISABLE=1` unless a diagnosed
hardware or driver problem requires it; those settings can reduce performance.

## Test the endpoint

Keep the vLLM launcher running. Open another terminal and log in to the head
node printed by the launcher. Run the Epi application on that same head node
when its custom endpoint remains `http://127.0.0.1:8000/v1`.

```bash
ssh HEAD_NODE

python <<'PY'
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="EMPTY",
)

model = client.models.list().data[0].id
print("Using model:", model)

response = client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Say hello and confirm the client works."}],
)
print(response.choices[0].message.content)
PY
```
