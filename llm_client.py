"""Provider adapters for Symbiote's tool-calling agent loop."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


class ProviderQuotaError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class ProviderRateLimitError(ProviderQuotaError):
    pass


class ProviderConfigError(Exception):
    pass


MODEL_PRESETS = {
    "gemini": {
        "fast": "gemini-2.5-flash",
        "pro": "gemini-2.5-pro",
    },
    "groq": {
        "fast": "llama-3.3-70b-versatile",
        "reasoning": "deepseek-r1-distill-llama-70b",
        "coding": "moonshotai/kimi-k2-instruct",
    },
}


def resolve_model(provider: str, model_or_preset: str | None) -> str:
    provider = provider.lower()
    if not model_or_preset:
        return MODEL_PRESETS[provider]["fast"]
    return MODEL_PRESETS.get(provider, {}).get(model_or_preset, model_or_preset)


def _retry_delay_from(err: Any) -> float | None:
    msg = str(err)
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", msg)
    if m:
        return min(300.0, float(m.group(1)) + 1.0)
    m = re.search(r"retry[_ -]?after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", msg, re.I)
    if m:
        return min(300.0, float(m.group(1)))
    return None


def _is_quota_or_rate_error(err: Any) -> bool:
    s = str(err).lower()
    markers = ("429", "resource_exhausted", "quota", "rate limit", "ratelimit", "retrydelay")
    return any(m in s for m in markers)


def _to_jsonable(v: Any) -> Any:
    if hasattr(v, "items"):
        return {k: _to_jsonable(vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)) or (hasattr(v, "__iter__") and not isinstance(v, (str, bytes))):
        try:
            return [_to_jsonable(x) for x in v]
        except TypeError:
            pass
    return v


def normalize_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, call in enumerate(calls):
        name = call.get("name") or call.get("function", {}).get("name")
        args = call.get("arguments")
        if args is None and isinstance(call.get("function"), dict):
            args = call["function"].get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                args = {}
        out.append({
            "id": call.get("id") or f"call_{i}_{name}",
            "name": name,
            "arguments": _to_jsonable(args or {}),
        })
    return [c for c in out if c["name"]]


def convert_tools_to_groq_openai_schema(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for tool in tools
    ]


@dataclass
class BaseLLMClient:
    model: str
    system_prompt: str

    provider: str = "base"

    def run_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError


class GeminiLLMClient(BaseLLMClient):
    provider = "gemini"

    def __init__(self, model: str, system_prompt: str, api_key: str | None = None):
        api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ProviderConfigError("GEMINI_API_KEY is not set")
        super().__init__(model=model, system_prompt=system_prompt)
        from google import genai
        from google.genai import types
        self._genai = genai
        self._types = types
        self._client = genai.Client(api_key=api_key)

    def _to_gemini_messages(self, messages: list[dict[str, Any]]):
        types = self._types
        contents = []
        for msg in messages:
            role = msg.get("role")
            if role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=msg.get("content", ""))]))
            elif role == "assistant":
                parts = []
                if msg.get("content"):
                    parts.append(types.Part(text=msg["content"]))
                for call in msg.get("tool_calls") or []:
                    parts.append(types.Part(function_call=types.FunctionCall(
                        name=call["name"],
                        args=call.get("arguments") or {},
                    )))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
            elif role == "tool":
                try:
                    payload = json.loads(msg.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {"result": msg.get("content", "")}
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_function_response(
                        name=msg.get("name", "tool"),
                        response=payload,
                    )],
                ))
        return contents

    def run_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if os.environ.get("SYMBIOTE_FORCE_GEMINI_QUOTA"):
            raise ProviderQuotaError("forced Gemini quota for testing", retry_after=60)

        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=self.system_prompt,
            tools=[types.Tool(function_declarations=tools)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=self._to_gemini_messages(messages),
                config=config,
            )
        except Exception as e:
            if _is_quota_or_rate_error(e):
                raise ProviderQuotaError(str(e), retry_after=_retry_delay_from(e)) from e
            raise

        text = ""
        calls = []
        cand_list = getattr(resp, "candidates", None) or []
        for cand in cand_list:
            parts = getattr(getattr(cand, "content", None), "parts", None) or []
            for part in parts:
                if getattr(part, "text", None):
                    text += part.text
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    calls.append({
                        "name": fc.name,
                        "arguments": _to_jsonable(getattr(fc, "args", {}) or {}),
                    })
        if not text and getattr(resp, "text", None):
            text = resp.text or ""
        return {"text": text, "tool_calls": normalize_tool_calls(calls)}


class GroqLLMClient(BaseLLMClient):
    provider = "groq"

    def __init__(self, model: str, system_prompt: str, api_key: str | None = None):
        api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise ProviderConfigError("GROQ_API_KEY is not set")
        super().__init__(model=model, system_prompt=system_prompt)
        try:
            from groq import Groq
        except ImportError as e:
            raise ProviderConfigError("groq package is not installed. Run pip install -r requirements.txt") from e
        self._client = Groq(api_key=api_key)

    def _to_groq_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [{"role": "system", "content": self.system_prompt}]
        for msg in messages:
            role = msg.get("role")
            if role == "user":
                out.append({"role": "user", "content": msg.get("content", "")})
            elif role == "assistant":
                item = {"role": "assistant", "content": msg.get("content") or ""}
                calls = []
                for call in msg.get("tool_calls") or []:
                    calls.append({
                        "id": call.get("id") or f"call_{call['name']}",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    })
                if calls:
                    item["tool_calls"] = calls
                out.append(item)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id") or f"call_{msg.get('name', 'tool')}",
                    "name": msg.get("name", "tool"),
                    "content": msg.get("content", ""),
                })
        return out

    def run_turn(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=self._to_groq_messages(messages),
                tools=convert_tools_to_groq_openai_schema(tools),
                tool_choice="auto",
            )
        except Exception as e:
            if _is_quota_or_rate_error(e):
                raise ProviderRateLimitError(str(e), retry_after=_retry_delay_from(e)) from e
            raise

        msg = resp.choices[0].message
        text = msg.content or ""
        calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            calls.append({
                "id": getattr(tc, "id", None),
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
        return {"text": text, "tool_calls": normalize_tool_calls(calls)}
