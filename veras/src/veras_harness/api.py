"""Provider-neutral OpenAI-compatible API configuration."""

from __future__ import annotations

import json
import os


DEFAULT_API_KEY_ENV = "VERAS_API_KEY"
PRIVATE_METADATA_KEYS = {"base_url", "credential_source", "model_provider"}


def model_from_environment(
    model: str,
    base_url: str,
    api_key_env: str = DEFAULT_API_KEY_ENV,
) -> dict[str, str]:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"{api_key_env} is required")
    return {
        "api_key": api_key,
        "api_key_env": api_key_env,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def codex_config(
    model: dict[str, str],
    *,
    reasoning_effort: str,
    context_window: int,
    auto_compact_token_limit: int,
    wire_api: str,
) -> str:
    q = json.dumps
    key_env = model["api_key_env"]
    return (
        f"model = {q(model['model'])}\n"
        'model_provider = "user_api"\n'
        f"model_reasoning_effort = {q(reasoning_effort)}\n"
        f"model_context_window = {context_window}\n"
        f"model_auto_compact_token_limit = {auto_compact_token_limit}\n"
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        "disable_response_storage = true\n"
        'web_search = "disabled"\n\n'
        "[features]\n"
        "plugins = false\n\n"
        "[shell_environment_policy]\n"
        'inherit = "core"\n'
        "ignore_default_excludes = false\n"
        f'exclude = [{q(key_env)}, "*KEY*", "*SECRET*", "*TOKEN*"]\n\n'
        "[model_providers.user_api]\n"
        'name = "User-configured OpenAI-compatible API"\n'
        f"base_url = {q(model['base_url'])}\n"
        f"env_key = {q(key_env)}\n"
        f"wire_api = {q(wire_api)}\n"
    )


def public_result_metadata(value: object, parent: str | None = None) -> object:
    """Remove connection details and upstream release hashes before persistence."""
    if isinstance(value, dict):
        return {
            key: public_result_metadata(item, key)
            for key, item in value.items()
            if key not in PRIVATE_METADATA_KEYS
            and not (parent == "harness_release" and key == "commit")
        }
    if isinstance(value, list):
        return [public_result_metadata(item, parent) for item in value]
    return value
