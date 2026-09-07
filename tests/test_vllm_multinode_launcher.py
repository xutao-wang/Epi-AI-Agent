from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
MULTI_NODE_LAUNCHER = (
    PROJECT_ROOT / "config/vllm_amarel/load_llm_with_vllm_multi_node.sh"
)
SINGLE_NODE_LAUNCHER = PROJECT_ROOT / "config/vllm_amarel/load_llm_with_vllm.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_profile(path: Path, *, tensor_parallel_size: int = 4) -> None:
    path.write_text(
        json.dumps(
            {
                "org/model": {
                    "vllm": {
                        "tensor_parallel_size": tensor_parallel_size,
                        "max_model_len": 32768,
                        "gpu_memory_utilization": 0.9,
                        "dtype": "auto",
                        "kv_cache_dtype": "auto",
                        "enforce_eager": False,
                        "enable_auto_tool_choice": True,
                        "tool_call_parser": "hermes",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_multinode_launcher_maps_one_rank_to_each_allocated_node(tmp_path) -> None:
    profile_path = tmp_path / "model_profiles.json"
    _write_profile(profile_path)
    captured = tmp_path / "srun-arguments.txt"
    fake_scontrol = tmp_path / "scontrol"
    _write_executable(
        fake_scontrol,
        "#!/usr/bin/env bash\n"
        "[[ $1 == show && $2 == hostnames ]] || exit 2\n"
        "printf '%s\\n' gpuk021 gpuk022\n",
    )
    fake_srun = tmp_path / "srun"
    _write_executable(
        fake_srun,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n",
    )
    environ = {
        **os.environ,
        "REPORT_AGENT_MODEL_PROFILES_PATH": str(profile_path),
        "SCONTROL_BIN": str(fake_scontrol),
        "SRUN_BIN": str(fake_srun),
        "SLURM_JOB_ID": "12345",
        "SLURM_JOB_NODELIST": "gpuk[021-022]",
        "SLURM_CPUS_PER_TASK": "32",
        "SLURM_STEP_ID": "0",
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
    }

    result = subprocess.run(
        ["bash", str(MULTI_NODE_LAUNCHER), "image.sif", "org/model", "2"],
        cwd=PROJECT_ROOT,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert arguments[:8] == [
        "--nodes=2",
        "--ntasks=2",
        "--ntasks-per-node=1",
        "--cpus-per-task=32",
        "--gpus-per-task=4",
        "--kill-on-bad-exit=1",
        "--label",
        "--overlap",
    ]
    assert arguments[-8:] == [
        str(MULTI_NODE_LAUNCHER),
        "--node-task",
        "image.sif",
        "org/model",
        "2",
        "gpuk021",
        "29501",
        "4",
    ]


def test_multinode_launcher_defaults_tensor_parallel_size_to_one(tmp_path) -> None:
    profile_path = tmp_path / "model_profiles.json"
    profile_path.write_text(
        json.dumps({"org/model": {"vllm": {}}}),
        encoding="utf-8",
    )
    captured = tmp_path / "srun-arguments.txt"
    fake_scontrol = tmp_path / "scontrol"
    _write_executable(
        fake_scontrol,
        "#!/usr/bin/env bash\nprintf '%s\\n' gpuk021\n",
    )
    fake_srun = tmp_path / "srun"
    _write_executable(
        fake_srun,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n",
    )
    environ = {
        **os.environ,
        "REPORT_AGENT_MODEL_PROFILES_PATH": str(profile_path),
        "SCONTROL_BIN": str(fake_scontrol),
        "SRUN_BIN": str(fake_srun),
        "SLURM_JOB_ID": "12345",
        "SLURM_JOB_NODELIST": "gpuk021",
        "SLURM_CPUS_PER_TASK": "32",
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
    }

    result = subprocess.run(
        ["bash", str(MULTI_NODE_LAUNCHER), "image.sif", "org/model", "1"],
        cwd=PROJECT_ROOT,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert "--gpus-per-task=1" in arguments
    assert arguments[-1] == "1"


def test_multinode_launcher_rejects_node_count_outside_allocation(tmp_path) -> None:
    profile_path = tmp_path / "model_profiles.json"
    _write_profile(profile_path)
    fake_scontrol = tmp_path / "scontrol"
    _write_executable(
        fake_scontrol,
        "#!/usr/bin/env bash\nprintf '%s\\n' gpuk021 gpuk022\n",
    )
    environ = {
        **os.environ,
        "REPORT_AGENT_MODEL_PROFILES_PATH": str(profile_path),
        "SCONTROL_BIN": str(fake_scontrol),
        "SLURM_JOB_ID": "12345",
        "SLURM_JOB_NODELIST": "gpuk[021-022]",
    }

    result = subprocess.run(
        ["bash", str(MULTI_NODE_LAUNCHER), "image.sif", "org/model", "3"],
        cwd=PROJECT_ROOT,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "requested 3 nodes, but the allocation contains 2" in result.stderr


def test_multinode_worker_builds_rank_specific_vllm_arguments(tmp_path) -> None:
    captured = tmp_path / "single-launcher-arguments.txt"
    captured_env = tmp_path / "single-launcher-environment.txt"
    fake_single_launcher = tmp_path / "single-launcher"
    _write_executable(
        fake_single_launcher,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n"
        "printf '%s\\n' \"${NCCL_SOCKET_IFNAME-}\" \"${GLOO_SOCKET_IFNAME-}\" "
        "  > \"$VLLM_ENV_CAPTURE_PATH\"\n",
    )
    environ = {
        **os.environ,
        "CUDA_VISIBLE_DEVICES": "0,1,2,3",
        "SLURM_PROCID": "1",
        "VLLM_NETWORK_IFACE": "ib0",
        "VLLM_SINGLE_NODE_LAUNCHER": str(fake_single_launcher),
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
        "VLLM_ENV_CAPTURE_PATH": str(captured_env),
    }

    result = subprocess.run(
        [
            "bash",
            str(MULTI_NODE_LAUNCHER),
            "--node-task",
            "image.sif",
            "org/model",
            "2",
            "gpuk021",
            "29501",
            "4",
        ],
        cwd=PROJECT_ROOT,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert captured.read_text(encoding="utf-8").splitlines() == [
        "image.sif",
        "org/model",
        "--distributed-executor-backend",
        "mp",
        "--nnodes",
        "2",
        "--node-rank",
        "1",
        "--master-addr",
        "gpuk021",
        "--master-port",
        "29501",
        "--pipeline-parallel-size",
        "2",
        "--headless",
    ]
    assert captured_env.read_text(encoding="utf-8").splitlines() == ["ib0", "ib0"]


def test_single_node_launcher_forwards_distributed_args_and_gpu_environment(
    tmp_path,
) -> None:
    profile_path = tmp_path / "model_profiles.json"
    _write_profile(profile_path)
    app_root = tmp_path / "apps"
    image = app_root / "singularity_images/image.sif"
    image.parent.mkdir(parents=True)
    image.touch()
    (app_root / "hf_cache").mkdir()
    (app_root / "vllm_addons").mkdir()
    captured = tmp_path / "apptainer-arguments.txt"
    fake_apptainer = tmp_path / "apptainer"
    _write_executable(
        fake_apptainer,
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" > \"$VLLM_LAUNCH_CAPTURE_PATH\"\n",
    )
    environ = {
        **os.environ,
        "APPTAINER_BIN": str(fake_apptainer),
        "CUDA_VISIBLE_DEVICES": "0,1,2,3",
        "NCCL_SOCKET_IFNAME": "ib0",
        "GLOO_SOCKET_IFNAME": "ib0",
        "REPORT_AGENT_MODEL_PROFILES_PATH": str(profile_path),
        "VLLM_APP_ROOT": str(app_root),
        "VLLM_LAUNCH_CAPTURE_PATH": str(captured),
    }

    result = subprocess.run(
        [
            "bash",
            str(SINGLE_NODE_LAUNCHER),
            image.name,
            "org/model",
            "--nnodes",
            "2",
            "--node-rank",
            "0",
        ],
        cwd=PROJECT_ROOT,
        env=environ,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    arguments = captured.read_text(encoding="utf-8").splitlines()
    assert "CUDA_VISIBLE_DEVICES=0,1,2,3" in arguments
    assert "NCCL_SOCKET_IFNAME=ib0" in arguments
    assert "GLOO_SOCKET_IFNAME=ib0" in arguments
    assert "--enforce-eager" not in arguments
    assert arguments[-6:] == [
        "--tool-call-parser",
        "hermes",
        "--nnodes",
        "2",
        "--node-rank",
        "0",
    ]
