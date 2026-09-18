"""OpenCode Go client with dynamic DeepSeek model selection."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.request
from typing import Iterable, Literal


ReasoningEffort = Literal["low", "medium", "high"]
AUTO_FLASH_MODEL = "latest-deepseek-flash"
AUTO_PRO_MODEL = "latest-deepseek-pro"
CURRENT_FLASH_MODEL = "deepseek-v4.1-flash"
CURRENT_PRO_MODEL = "deepseek-v4-pro"
DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 2.0
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
DEEPSEEK_MODEL_RE = re.compile(r"^deepseek-v(\d+(?:\.\d+)*?)-(flash|pro)$")
REASONING_MODEL_PREFIXES = ("deepseek-v", "deepseek-r1")
USER_AGENT = "coleta-goldman-podcasts/0.1"


@dataclass(frozen=True)
class OpenCodeConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    prompt_builder_model: str = AUTO_FLASH_MODEL
    summarizer_model: str = AUTO_PRO_MODEL


class RetryableRequestError(RuntimeError):
    pass


def latest_deepseek_model(model_ids: Iterable[str], variant: str) -> str:
    candidates = []
    for model_id in model_ids:
        match = DEEPSEEK_MODEL_RE.fullmatch(model_id)
        if match and match.group(2) == variant:
            version = tuple(int(part) for part in match.group(1).split("."))
            candidates.append((version, model_id))
    if not candidates:
        raise RuntimeError(f"OpenCode returned no versioned DeepSeek {variant} model")
    return max(candidates)[1]


def list_models(config: OpenCodeConfig, timeout: int = 30) -> tuple[str, ...]:
    url = config.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.api_key}",
            "User-Agent": USER_AGENT,
            "x-opencode-session": hashlib.sha256(url.encode()).hexdigest(),
        },
        method="GET",
    )
    response = json.loads(open_request(request, timeout))
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("OpenCode returned a malformed model list")
    return tuple(
        str(item["id"])
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )


def resolve_models(config: OpenCodeConfig) -> OpenCodeConfig:
    needs_flash = config.prompt_builder_model == AUTO_FLASH_MODEL
    needs_pro = config.summarizer_model == AUTO_PRO_MODEL
    if not needs_flash and not needs_pro:
        return config
    try:
        model_ids = list_models(config)
        flash_model = (
            latest_deepseek_model(model_ids, "flash")
            if needs_flash
            else config.prompt_builder_model
        )
        pro_model = (
            latest_deepseek_model(model_ids, "pro")
            if needs_pro
            else config.summarizer_model
        )
    except Exception as exc:
        print(
            f"OpenCode model discovery failed ({exc.__class__.__name__}); using fallback models."
        )
        flash_model = CURRENT_FLASH_MODEL if needs_flash else config.prompt_builder_model
        pro_model = CURRENT_PRO_MODEL if needs_pro else config.summarizer_model
    return replace(
        config,
        prompt_builder_model=flash_model,
        summarizer_model=pro_model,
    )


def post_json(
    url: str,
    headers: dict,
    payload: dict,
    *,
    timeout: int = 60,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff: float = DEFAULT_RETRY_BACKOFF,
) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return open_request(request, timeout)
        except RetryableRequestError as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            delay = backoff * (2**attempt) * (1 + 0.2 * random.random())
            time.sleep(delay)
    raise RuntimeError(
        f"Request to {url} failed after {max_retries + 1} attempts: {last_error}"
    ) from last_error


def chat_completion(
    config: OpenCodeConfig,
    *,
    model: str,
    messages: Iterable[dict],
    json_mode: bool = False,
    reasoning_effort: ReasoningEffort | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    timeout: int = 300,
) -> str:
    payload: dict = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if reasoning_effort and any(model.startswith(p) for p in REASONING_MODEL_PREFIXES):
        payload["reasoning_effort"] = reasoning_effort
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    session_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    response_text = post_json(
        config.base_url.rstrip("/") + "/chat/completions",
        {
            "Authorization": f"Bearer {config.api_key}",
            "User-Agent": USER_AGENT,
            "x-opencode-session": session_id,
        },
        payload,
        timeout=timeout,
    )
    response = json.loads(response_text)
    try:
        choice = response["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected OpenCode response shape: {response_text[:1000]}") from exc
    if choice.get("finish_reason") == "length":
        limit = f"{max_tokens}-token" if max_tokens else "provider"
        raise RuntimeError(f"OpenCode response reached the {limit} output limit")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("OpenCode returned empty content")
    return content.strip()


def open_request(request: urllib.request.Request, timeout: int) -> str:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in RETRYABLE_HTTP_STATUS:
            raise RetryableRequestError(f"HTTP {exc.code}: {body[:300]}") from exc
        raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RetryableRequestError(f"Request failed: {exc.reason}") from exc
    except OSError as exc:
        raise RetryableRequestError(f"Request failed: {exc}") from exc
