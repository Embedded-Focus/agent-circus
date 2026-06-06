"""Tests for llama.cpp integration: override generation and OpenCode provider injection."""

import json
from pathlib import Path

import pytest

from agent_circus.config import (
    LLAMA_CPP_CONTAINER_MODELS_PATH,
    LLAMA_CPP_DEFAULT_CONTEXT_SIZE,
    LLAMA_CPP_DEFAULT_MODEL,
    LLAMA_CPP_DEFAULT_MODELS_CACHE,
    LLAMA_CPP_IMAGE,
    LLAMA_CPP_PORT,
    build_agent_config_additions,
    build_llama_cpp_override,
)
from agent_circus.exceptions import ConfigurationError

# ---------------------------------------------------------------------------
# build_llama_cpp_override
# ---------------------------------------------------------------------------


class TestBuildLlamaCppOverride:
    def test_defaults(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        svc = result["services"]["llama-cpp"]

        assert svc["image"] == LLAMA_CPP_IMAGE
        cmd = svc["command"]
        assert "--hf-repo" in cmd
        assert "--hf-file" in cmd
        repo_idx = cmd.index("--hf-repo")
        file_idx = cmd.index("--hf-file")
        assert cmd[repo_idx + 1] == "ggml-org/gemma-3-1b-it-GGUF"
        assert cmd[file_idx + 1] == "gemma-3-1b-it-Q4_K_M.gguf"

    def test_server_config_via_env_vars(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        env = result["services"]["llama-cpp"]["environment"]
        assert env["LLAMA_ARG_HOST"] == "0.0.0.0"
        assert env["LLAMA_ARG_PORT"] == str(LLAMA_CPP_PORT)
        assert env["LLAMA_ARG_CTX_SIZE"] == str(LLAMA_CPP_DEFAULT_CONTEXT_SIZE)

    def test_server_config_not_in_command(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        cmd = result["services"]["llama-cpp"]["command"]
        assert "--host" not in cmd
        assert "--port" not in cmd
        assert "--ctx-size" not in cmd

    def test_default_volume_mounts_huggingface_cache(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        svc = result["services"]["llama-cpp"]
        assert any(
            v.startswith(LLAMA_CPP_DEFAULT_MODELS_CACHE + ":") for v in svc["volumes"]
        )
        assert any(f":{LLAMA_CPP_CONTAINER_MODELS_PATH}:" in v for v in svc["volumes"])

    def test_default_hf_home_env(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        svc = result["services"]["llama-cpp"]
        assert svc["environment"]["HF_HOME"] == LLAMA_CPP_CONTAINER_MODELS_PATH

    def test_hf_path_split_into_repo_and_file(self) -> None:
        cfg = {"model": "myorg/myrepo/mymodel-Q8_0.gguf"}
        result = json.loads(build_llama_cpp_override(cfg))
        cmd = result["services"]["llama-cpp"]["command"]
        assert cmd[cmd.index("--hf-repo") + 1] == "myorg/myrepo"
        assert cmd[cmd.index("--hf-file") + 1] == "mymodel-Q8_0.gguf"

    def test_hf_path_without_file_uses_repo_only(self) -> None:
        cfg = {"model": "myorg/myrepo"}
        result = json.loads(build_llama_cpp_override(cfg))
        cmd = result["services"]["llama-cpp"]["command"]
        assert cmd[cmd.index("--hf-repo") + 1] == "myorg/myrepo"
        assert "--hf-file" not in cmd

    def test_absolute_local_path_uses_dash_m(self) -> None:
        cfg = {"model": "/models/mymodel.gguf"}
        result = json.loads(build_llama_cpp_override(cfg))
        cmd = result["services"]["llama-cpp"]["command"]
        assert cmd[cmd.index("-m") + 1] == "/models/mymodel.gguf"
        assert "--hf-repo" not in cmd

    def test_custom_context_size(self) -> None:
        result = json.loads(build_llama_cpp_override({"context_size": 4096}))
        assert (
            result["services"]["llama-cpp"]["environment"]["LLAMA_ARG_CTX_SIZE"]
            == "4096"
        )

    def test_custom_models_cache(self) -> None:
        cfg = {"models_cache": "/data/models"}
        result = json.loads(build_llama_cpp_override(cfg))
        volumes = result["services"]["llama-cpp"]["volumes"]
        assert any(v.startswith("/data/models:") for v in volumes)

    def test_extra_args_appended(self) -> None:
        cfg = {"extra_args": ["--n-predict", "512"]}
        result = json.loads(build_llama_cpp_override(cfg))
        cmd = result["services"]["llama-cpp"]["command"]
        assert "--n-predict" in cmd
        assert "512" in cmd

    def test_opencode_depends_on_llama_cpp(self) -> None:
        result = json.loads(build_llama_cpp_override({}))
        assert "llama-cpp" in result["services"]["opencode"]["depends_on"]

    def test_tilde_models_cache_raises(self) -> None:
        cfg = {"models_cache": "~/models"}
        with pytest.raises(ConfigurationError, match="~"):
            build_llama_cpp_override(cfg)

    def test_compose_env_interpolation_in_models_cache_is_accepted(self) -> None:
        cfg = {"models_cache": "${HOME}/.cache/models"}
        result = json.loads(build_llama_cpp_override(cfg))
        volumes = result["services"]["llama-cpp"]["volumes"]
        assert any(v.startswith("${HOME}/.cache/models:") for v in volumes)


# ---------------------------------------------------------------------------
# build_agent_config_additions — llama.cpp provider injection
# ---------------------------------------------------------------------------


class TestBuildAgentConfigAdditionsLlamaCpp:
    def test_no_llama_cpp_config_returns_empty(self) -> None:
        result = build_agent_config_additions({})
        assert result == {}

    def test_llama_cpp_adds_opencode_provider(self) -> None:
        cfg = {"llama_cpp": {}}
        result = build_agent_config_additions(cfg)
        provider = result["opencode"]["provider"]["llama.cpp"]
        assert provider["npm"] == "@ai-sdk/openai-compatible"
        assert provider["options"]["baseURL"] == f"http://llama-cpp:{LLAMA_CPP_PORT}/v1"

    def test_model_id_derived_from_hf_file_stem(self) -> None:
        cfg = {"llama_cpp": {"model": "org/repo/mymodel-Q4_K_M.gguf"}}
        result = build_agent_config_additions(cfg)
        models = result["opencode"]["provider"]["llama.cpp"]["models"]
        assert "mymodel-Q4_K_M" in models

    def test_default_model_id_derived_from_default_model(self) -> None:
        cfg = {"llama_cpp": {}}
        result = build_agent_config_additions(cfg)
        expected_id = Path(LLAMA_CPP_DEFAULT_MODEL).stem
        models = result["opencode"]["provider"]["llama.cpp"]["models"]
        assert expected_id in models

    def test_context_size_reflected_in_model_limit(self) -> None:
        cfg = {"llama_cpp": {"context_size": 8192}}
        result = build_agent_config_additions(cfg)
        model_id = list(result["opencode"]["provider"]["llama.cpp"]["models"])[0]
        limit = result["opencode"]["provider"]["llama.cpp"]["models"][model_id]["limit"]
        assert limit["context"] == 8192
        assert limit["output"] == 8192

    def test_llama_cpp_and_mcp_both_injected_into_opencode(self) -> None:
        cfg = {
            "llama_cpp": {},
            "mcp_servers": [
                {"name": "myserver", "url": "http://localhost:9000"},
            ],
        }
        result = build_agent_config_additions(cfg)
        opencode = result["opencode"]
        assert "mcp" in opencode
        assert "provider" in opencode

    def test_llama_cpp_does_not_affect_other_agents(self) -> None:
        cfg = {"llama_cpp": {}}
        result = build_agent_config_additions(cfg)
        assert "claude-code" not in result
        assert "codex" not in result
        assert "mistral-vibe" not in result
