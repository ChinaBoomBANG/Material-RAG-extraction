"""LLM 客户端：任何 OpenAI 兼容服务，默认指向本机 LM Studio。

设计要点：
  - 只依赖标准库 urllib，不引入 openai 包，减少依赖面；
  - model 留空时自动从 /v1/models 取第一个模型，省去手工配置；
  - 提供 chat() 与 chat_json()；后者负责从模型输出里稳妥地抠出 JSON
    （模型常有 ```json 包裹、前后缀解释文字等），并对解析失败做一次重试。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from .. import config as cfg

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, conf: dict | None = None):
        self.conf = conf or cfg.load()
        self.base_url = self.conf["llm"]["base_url"].rstrip("/")
        self.api_key = self.conf["llm"].get("api_key", "")
        self.model = self.conf["llm"].get("model", "") or ""
        self.gen = self.conf["llm"]["gen"]
        self._resolved = bool(self.model)

    # ---------------------------------------------------------- 基础

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        t = timeout or int(self.gen.get("timeout_s", 600))
        last_err: Exception | None = None
        for attempt in range(int(self.gen.get("retries", 2)) + 1):
            try:
                with urllib.request.urlopen(req, timeout=t) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:500]
                except Exception:
                    pass
                last_err = LLMError(f"HTTP {e.code} {e.reason} | {body}")
                if e.code < 500:
                    break
            except Exception as e:  # 连接失败、超时
                last_err = e
            time.sleep(1.5 * (attempt + 1))
        raise LLMError(
            f"调用 {url} 失败：{last_err}\n"
            f"请确认 LM Studio 已启动并开启本地服务（默认 http://127.0.0.1:1234）。"
        )

    # ---------------------------------------------------------- 模型发现

    def list_models(self) -> list[str]:
        req = urllib.request.Request(f"{self.base_url}/models", headers=self._headers())
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return [m.get("id", "") for m in data.get("data", [])]

    def resolve_model(self) -> str:
        if not self._resolved:
            ids = self.list_models()
            if not ids:
                raise LLMError("LM Studio 未返回任何已加载模型，请先在 LM Studio 中加载模型。")
            self.model = ids[0]
            self._resolved = True
        return self.model

    def health(self) -> dict:
        try:
            ids = self.list_models()
            return {"ok": True, "base_url": self.base_url,
                    "models": ids, "using": self.model or (ids[0] if ids else None)}
        except Exception as e:
            return {"ok": False, "base_url": self.base_url, "error": str(e)}

    # ---------------------------------------------------------- 生成

    def chat(self, user: str, system: str = "", temperature: float | None = None,
             max_tokens: int | None = None,
             response_format: dict | None = None) -> str:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        payload = {
            "model": self.resolve_model(),
            "messages": msgs,
            "temperature": self.gen.get("temperature", 0.1) if temperature is None else temperature,
            "top_p": self.gen.get("top_p", 0.9),
            "max_tokens": int(self.gen.get("max_tokens", 2048)) if max_tokens is None else max_tokens,
            "stream": False,
        }
        # 推理型模型（如 gemma-4）会把全部 token 预算耗在内部思考上，正文一个字
        # 都出不来。reasoning_effort="none" 可让 LM Studio 关掉思考；
        # 配空字符串则不发送该字段，兼容不支持它的服务端。
        effort = str(self.gen.get("reasoning_effort", "") or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        # 结构化输出：云端 API 支持 response_format（json_object），能显著减少
        # "模型输出带解释文字/Markdown 围栏" 引发的解析失败。部分本地服务不支持，
        # 返回 4xx 时自动去掉该字段重试一次，保证兼容性。
        if response_format:
            payload["response_format"] = response_format
        try:
            resp = self._post("/chat/completions", payload)
        except LLMError as e:
            if response_format and "HTTP 4" in str(e):
                payload.pop("response_format", None)
                resp = self._post("/chat/completions", payload)
            else:
                raise
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            # 带上实际请求地址：最常见的坑是 base_url 少了 /v1，
            # 服务端会回 "Unexpected endpoint or method"，光看响应体根本看不出原因。
            hint = ""
            if not url.rstrip("/").endswith("/v1") and "/v1/" not in url:
                hint = (f"（提示：base_url={self.base_url!r} 似乎缺少 /v1，"
                        f"OpenAI 兼容服务通常要求 http://host:port/v1）")
            raise LLMError(
                f"返回结构异常：{json.dumps(resp, ensure_ascii=False)[:400]}"
                f"\n请求地址：{url}{hint}") from e

    def chat_json(self, user: str, system: str = "", retry_hint: str = "",
                  response_format: dict | None = None) -> dict:
        """要求模型输出 JSON，并稳健解析。

        response_format 传入 ``{"type": "json_object"}`` 时走结构化输出通道
        （抽取用）；不传则维持原有纯提示词约束（问答等场景行为不变）。
        """
        text = self.chat(user, system=system, temperature=0.0,
                         response_format=response_format)
        obj = extract_json(text)
        if obj is not None:
            return obj
        # 一次修复性重试：把错误输出回喂，要求只输出 JSON
        fix_user = (f"{user}\n\n注意：你上一次的输出不是合法 JSON。"
                    f"请只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码块。"
                    f"{(' ' + retry_hint) if retry_hint else ''}")
        text2 = self.chat(fix_user, system=system, temperature=0.0,
                          response_format=response_format)
        obj2 = extract_json(text2)
        if obj2 is None:
            raise LLMError(f"模型未返回可解析的 JSON。原始输出前 500 字：\n{text2[:500]}")
        return obj2


# ---------------------------------------------------------------- JSON 解析

def extract_json(text: str) -> dict | None:
    """从模型输出中抠出第一个完整 JSON 对象。"""
    if not text:
        return None
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 括号配平扫描：从第一个 { 开始找匹配的 }
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None
