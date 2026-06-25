from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import requests


class ChatClient(Protocol):
    def complete(self, *, system: str, user: str) -> str:
        ...


@dataclass(frozen=True)
class DpskChatClient:
    """DeepSeek-compatible chat-completion client.

    The API key is read from the process environment by name. It is not logged,
    serialized, or written to any artifact.
    """

    api_key_env: str = "DPSK_API_KEY"
    base_url_env: str = "DPSK_BASE_URL"
    model: str = "deepseek-chat"
    timeout_s: int = 90

    @property
    def base_url(self) -> str:
        return os.environ.get(self.base_url_env, "https://api.deepseek.com/chat/completions")

    def complete(self, *, system: str, user: str) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"{self.api_key_env} is not set")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        resp = requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"]).strip()
