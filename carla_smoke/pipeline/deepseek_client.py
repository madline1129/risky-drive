#!/usr/bin/env python3
"""Small DeepSeek chat-completions client for the CARLA risk pipeline."""

import json
import os
import urllib.error
import urllib.request


DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


class DeepSeekError(RuntimeError):
    pass


def find_env_file(start_dir=None, filename=".env"):
    current = os.path.abspath(start_dir or os.getcwd())
    if os.path.isfile(current):
        current = os.path.dirname(current)

    while True:
        candidate = os.path.join(current, filename)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_env_file(path=None, override=False):
    env_path = path or find_env_file()
    if not env_path or not os.path.exists(env_path):
        return None

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value
    return env_path


def get_api_key(env_name, env_file=None):
    # Prefer the project .env over a stale exported shell variable. This pipeline is
    # usually launched from long-lived conda shells where old API keys can linger.
    load_env_file(env_file, override=True)
    api_key = os.environ.get(env_name)
    if not api_key:
        hint = f" Put {env_name}=<your key> in .env or export it in the shell."
        raise DeepSeekError(f"Missing API key.{hint}")
    return api_key


def chat_json(url, model, api_key, prompt, timeout, temperature=0.2):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DeepSeekError(f"DeepSeek HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise DeepSeekError(f"DeepSeek request failed: {exc}") from exc

    choices = result.get("choices", [])
    if not choices:
        raise DeepSeekError(f"DeepSeek response has no choices: {result}")
    return choices[0].get("message", {}).get("content", "")


def parse_json_response(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
