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
  --time=2:00:00 \
  --pty bash
```

From the project root, launch the configured model:

```bash
image_name=vllm-openai_v0.19.1.sif
model_name=google/gemma-4-31B-it
bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name"
```

The matching `config/amarel_model_profiles.json` entry is optional. Its nested
`vllm` object is a set of overrides: only parameters that are present are
passed to `vllm serve`, and omitted parameters use the installed vLLM version's
defaults. Unknown or invalid parameters for the selected model are rejected;
entries for other models are ignored.

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

## Run and access the Epi application

On the vLLM head node, start the application from the project root:

```bash
python run_fastapi.py
```

Keep this terminal open while using the application. By default, the application
listens on `127.0.0.1:8080`.

On your local computer—not on Amarel—open another terminal and create an SSH
tunnel to the head node:

```bash
ssh -N -J <netid>@amarel.rutgers.edu \
  -L 8080:127.0.0.1:8080 \
  <netid>@<HEAD_NODE>
```

Replace `<netid>` with your Rutgers NetID and `<HEAD_NODE>` with the head node
printed by the vLLM launcher. Then open the following address in your browser:

<http://127.0.0.1:8080/>
