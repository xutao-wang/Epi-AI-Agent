# Running vLLM on Amarel

Run these launchers only after Slurm has allocated GPU resources. The examples
assume four GPUs and 240 GB of RAM per node in the lab partition.

## Single node

Request a GPU node:

```bash
salloc \
  --partition=p_wj183_1 \
  --nodes=1 \
  --job-name=LLM-test \
  --ntasks=1 \
  --cpus-per-task=32 \
  --gres=gpu:4 \
  --mem=240G \
  --time=2:00:00 \
  --no-shell

# login to the node
squeue -u $USER -o "%.18i %.9P %.20j %.2t %.10M %.30R"
srun --jobid=<JOBID> --pty bash -l
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

## Recommended Qwen profiles

The shipped Qwen profiles follow the model authors' deployment guidance and
the corresponding vLLM recipes. Both use their native 262,144-token context
window. This limit includes the prompt, retained conversation, model reasoning,
and final response.

### Qwen3-Next 80B A3B Instruct FP8

The [Qwen3-Next model card](https://huggingface.co/Qwen/Qwen3-Next-80B-A3B-Instruct-FP8)
and [vLLM recipe](https://recipes.vllm.ai/Qwen/Qwen3-Next-80B-A3B-Instruct)
recommend tensor parallelism across four GPUs for the FP8 checkpoint. The
profile also enables prefix caching and the recommended Hermes tool parser.
Qwen3-Next Instruct is a non-thinking model, so it does not need a reasoning
parser.

```bash
image_name=vllm-openai_v0.19.1.sif
model_name=Qwen/Qwen3-Next-80B-A3B-Instruct-FP8

bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name"
```

Multi-token prediction is optional. Test its memory use and throughput on the
allocated GPU type before making it a profile default:

```bash
bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name" \
  --speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":2}'
```

If the server cannot allocate the native context window, append
`--max-model-len 32768` to the launcher command as the model card's conservative
fallback. Explicit arguments placed after the model name take precedence over
profile arguments.

### Qwen3.8 27B FP8

Use the official pre-quantized `Qwen/Qwen3.8-27B-FP8` checkpoint. The
[Qwen3.8 model card](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) documents its
native vision support and 262,144-token context window. The
[vLLM recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B) recommends the `qwen3`
reasoning parser and FP8 KV cache for the four-GPU FP8 configuration; the
profile also enables automatic tool choice with the `qwen3_coder` parser.
Because the checkpoint already contains FP8 weights, the profile intentionally
does not pass `--quantization fp8`.

```bash
image_name=vllm-openai_v0.19.1.sif
model_name=Qwen/Qwen3.8-27B-FP8

bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name"
```

Qwen3.8 thinks by default. For direct non-thinking responses, send
`{"chat_template_kwargs":{"enable_thinking":false}}` in the request's
`extra_body`. Its optional built-in MTP draft head can be enabled at launch:

```bash
bash config/vllm_amarel/load_llm_with_vllm.sh \
  "$image_name" "$model_name" \
  --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
```

The vLLM recipes list vLLM 0.17.0 or newer for both models. The example 0.19.1
container satisfies that minimum; use a newer image if the installed build
reports model-architecture or parser compatibility errors.

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
source .venv/bin/activate # activate your virtual env
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
