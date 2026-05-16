#!/usr/bin/env python3
"""Small DeepSeek chat-completions client for the CARLA risk pipeline."""

import json
import os
import urllib.error
import urllib.request


DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


class DeepSeekError(RuntimeError):
    pass


def get_api_key(env_name):
    api_key = os.environ.get(env_name)
    if not api_key:
        raise DeepSeekError(f"Missing API key. Set {env_name}=<your DeepSeek API key>.")
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
