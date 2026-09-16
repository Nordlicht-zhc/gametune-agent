from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ModelAPIError(RuntimeError):
    """A sanitized model-provider error safe to display in the UI."""


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("role 必须是 system、user 或 assistant")
        if not self.content.strip():
            raise ValueError("消息内容不能为空")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    temperature: float = 0.2
    max_output_tokens: int = 800
    response_format: str = "json"
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("模型请求至少需要一条消息")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens 必须大于 0")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    raw_id: str | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """Small provider boundary used by the agent; implementations are replaceable."""

    @property
    def name(self) -> str: ...

    def complete(self, request: ModelRequest) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class APIConfig:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 30.0
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url.strip() or not self.api_key.strip() or not self.model.strip():
            raise ValueError("模型 API 地址、密钥和模型名称不能为空")
        if self.timeout_seconds <= 0:
            raise ValueError("模型 API 超时必须大于 0")
        if self.reasoning_effort not in {None, "low", "medium", "high"}:
            raise ValueError("思考模式必须是关闭、低、中或高")

    @classmethod
    def from_env(cls) -> APIConfig | None:
        base_url = os.getenv("GAMETUNE_API_BASE", "").strip().rstrip("/")
        api_key = os.getenv("GAMETUNE_API_KEY", "").strip()
        model = os.getenv("GAMETUNE_MODEL", "").strip()
        if not base_url and not api_key and not model:
            return None
        missing = [name for name, value in (("GAMETUNE_API_BASE", base_url), ("GAMETUNE_API_KEY", api_key), ("GAMETUNE_MODEL", model)) if not value]
        if missing:
            raise ValueError(f"模型 API 配置不完整：缺少 {', '.join(missing)}")
        timeout = float(os.getenv("GAMETUNE_API_TIMEOUT", "30"))
        if timeout <= 0:
            raise ValueError("GAMETUNE_API_TIMEOUT 必须大于 0")
        reasoning_effort = os.getenv("GAMETUNE_REASONING_EFFORT", "").strip().lower() or None
        return cls(base_url, api_key, model, timeout, reasoning_effort)


class OpenAICompatibleProvider:
    """Minimal Chat Completions-compatible adapter with no third-party dependency."""

    def __init__(self, config: APIConfig):
        self.config = config

    @property
    def name(self) -> str:
        return "openai-compatible"

    @staticmethod
    def list_models(base_url: str, api_key: str, timeout_seconds: float = 30.0) -> list[str]:
        """Return model ids exposed by an OpenAI-compatible ``/models`` endpoint."""
        normalized_base = str(base_url).strip().rstrip("/")
        normalized_key = str(api_key).strip()
        if not normalized_base or not normalized_key:
            raise ValueError("获取模型前请先填写 API 地址和 API 密钥")
        if timeout_seconds <= 0:
            raise ValueError("模型列表请求超时必须大于 0")
        http_request = urllib.request.Request(
            f"{normalized_base}/models",
            headers={"Authorization": f"Bearer {normalized_key}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=float(timeout_seconds)) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ModelAPIError(f"模型服务返回 HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ModelAPIError("无法获取模型列表，请检查地址、网络和超时设置") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelAPIError("模型服务返回了无法解析的模型列表") from error

        candidates = body.get("data") if isinstance(body, dict) else None
        if candidates is None and isinstance(body, dict):
            # A few compatible gateways use ``models`` instead of OpenAI's
            # standard ``data`` key; accepting it costs nothing for callers.
            candidates = body.get("models")
        if not isinstance(candidates, list):
            raise ModelAPIError("模型服务返回的模型列表格式无法识别")
        model_ids: set[str] = set()
        for item in candidates:
            value = item.get("id") if isinstance(item, dict) else item
            if isinstance(value, str) and value.strip():
                model_ids.add(value.strip())
        if not model_ids:
            raise ModelAPIError("模型服务未返回可用模型")
        return sorted(model_ids, key=str.casefold)

    def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": item.role, "content": item.content} for item in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        http_request = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.config.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ModelAPIError(f"模型服务返回 HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ModelAPIError("无法连接模型服务，请检查地址、网络和超时设置") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelAPIError("模型服务返回了无法解析的数据") from error
        try:
            choice = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelAPIError("模型响应缺少 choices[0].message.content") from error
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return ModelResponse(
            str(choice),
            str(body.get("model", self.config.model)),
            self.name,
            {str(key): int(value) for key, value in usage.items() if isinstance(value, int)},
            str(body["id"]) if body.get("id") else None,
        )


def provider_from_env() -> ModelProvider | None:
    config = APIConfig.from_env()
    return OpenAICompatibleProvider(config) if config else None
