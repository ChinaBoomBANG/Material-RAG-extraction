"""LLM 客户端：任何 OpenAI 兼容服务，默认指向本机 LM Studio。

设计要点：
  - 只依赖标准库 urllib，不引入 openai 包，减少依赖面；
  - model 留空时自动从 /v1/models 取第一个模型，省去手工配置；
  - 提供 chat() 与 chat_json()；后者负责从模型输出里稳妥地抠出 JSON
    （模型常有 ```json 包裹、前后缀解释文字等），并对解析失败做一次重试。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config as cfg

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


class LLMError(RuntimeError):
    pass


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# 健康检查短缓存：前端会周期性拉 /api/status，每次都探测端口 + 请求 /models
# 属于重复劳动（模型是否在线不可能毫秒级反复变化）。缓存几秒即可。
_HEALTH_CACHE: dict = {"key": None, "ts": 0.0, "val": None}
_HEALTH_TTL = 3.0


def opener_for(url: str):
    """按目标地址选择 opener：本地地址**不走代理**。

    为什么必须这样做：Windows 上设了系统代理后，urllib 会把 127.0.0.1 也交给代理
    （实测 `proxy_bypass('127.0.0.1:1234')` 返回 False）。模型服务在线时代理能转发，
    一旦模型没启动，请求就在代理里干等，把界面状态接口和启动轮询一起拖死。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def _probe_port(url: str, timeout: float | None = None) -> str | None:
    """快速探测 TCP 端口是否可连。可连返回 None，不可连通返回原因字符串。

    用途：避免为"服务根本没起来"这种情况付出 HTTP 层的整套超时代价。

    超时按目标区分：本机回环要么立刻连上、要么立刻被拒（毫秒级），
    给 0.1s 足够；若统一用 0.35s，模型没启动时每次状态查询都要白等 0.35 秒
    （实测 /api/status 每次都慢到 0.36s 就是这里）。远端网络可能真有延迟，给 1.5s。
    """
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "127.0.0.1").lower()
    port = u.port or (443 if u.scheme == "https" else 80)
    if timeout is None:
        timeout = 0.1 if host in _LOCAL_HOSTS else 1.5
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            if s.connect_ex((host, port)) != 0:
                return f"无法连接 {host}:{port}（服务未启动？）"
    except OSError as e:
        return f"连接 {host}:{port} 失败：{e}"
    return None


class LLMClient:
    def __init__(self, conf: dict | None = None):
        self.conf = conf or cfg.load()
        self.base_url = self.conf["llm"]["base_url"].rstrip("/")
        # 密钥优先级：环境变量 > 配置文件。
        # 仓库是公开的，把真密钥写进 config.yaml 极易误提交；设 RAG_API_KEY 即可。
        self.api_key = os.environ.get("RAG_API_KEY") or self.conf["llm"].get("api_key", "")
        self.model = self.conf["llm"].get("model", "") or ""
        self.gen = self.conf["llm"]["gen"]
        self._resolved = bool(self.model)

    # ---------------------------------------------------------- 基础

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> dict:
        url = self._url(path)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        t = timeout or int(self.gen.get("timeout_s", 600))
        opener = opener_for(url)
        last_err: Exception | None = None
        for attempt in range(int(self.gen.get("retries", 2)) + 1):
            # 重试时重新构造 Request：RequestBody 可能已被上一次尝试消费。
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                with opener.open(req, timeout=t) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "ignore")[:500]
                except Exception:
                    pass
                last_err = LLMError(f"HTTP {e.code} {e.reason} | {body}")
                if e.code == 429:
                    # 云端配额限流是可恢复的：等更久再试。原来按"4xx 不重试"直接放弃，
                    # 结果一次限流就把整批抽抽取打成失败（界面只显示旧数据）。
                    time.sleep(5 * (attempt + 1))
                    continue
                if e.code < 500:
                    break
            except Exception as e:  # 连接失败、超时
                last_err = e
            time.sleep(1.5 * (attempt + 1))
        hint = ("云端返回 429：并发超过配额了，把 extract.workers 调小再试。\n"
                if last_err and "429" in str(last_err) else
                "请确认 LM Studio 已启动并开启本地服务（默认 http://127.0.0.1:1234）。"
                if "127.0.0.1" in url or "localhost" in url else
                "请确认云端 API 地址、密钥与模型名是否正确。")
        raise LLMError(f"调用 {url} 失败：{last_err}\n{hint}")

    # ---------------------------------------------------------- 模型发现

    def list_models(self, timeout: int = 15) -> list[str]:
        url = self._url("/models")
        req = urllib.request.Request(url, headers=self._headers())
        with opener_for(url).open(req, timeout=timeout) as r:
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

    def health(self, ttl: float = _HEALTH_TTL) -> dict:
        key = (self.base_url, self.model)
        now = time.time()
        if (_HEALTH_CACHE["val"] is not None and _HEALTH_CACHE["key"] == key
                and now - _HEALTH_CACHE["ts"] < ttl):
            return _HEALTH_CACHE["val"]
        # 端口探测只对本地回环做：本地服务没起来时 HTTP 连接要等约 2 秒才报错，
        # 每次状态查询都被这 2 秒拖住（启动脚本轮询跟着超时）。
        # 云端不能这样探：那需要真的走网络/代理，裸 socket 探测会误判成"连不上"
        # （实测云端健康检查本身只要 0.28 秒，直接请求更准也更快）。
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        probe = _probe_port(self.base_url) if host in _LOCAL_HOSTS else None
        if probe is not None:
            val = {"ok": False, "base_url": self.base_url, "error": probe}
        else:
            try:
                ids = self.list_models(timeout=6 if host not in _LOCAL_HOSTS else 15)
                val = {"ok": True, "base_url": self.base_url,
                       "models": ids, "using": self.model or (ids[0] if ids else None)}
            except Exception as e:
                val = {"ok": False, "base_url": self.base_url, "error": str(e)}
        _HEALTH_CACHE.update({"key": key, "ts": now, "val": val})
        return val

    # ---------------------------------------------------------- 生成

    def _stream_post(self, path: str, payload: dict, timeout: int | None = None) -> str:
        """流式请求，边收边拼。

        为什么必须支持流式：非流式时服务端会把整个回复生成完才返回响应头，
        客户端全程卡在 `_read_status` 上毫无反馈。实测云端返回一个 101 字段的
        JSON 要 5 分钟以上，界面看起来就是"卡死"，也无法判断是在算还是断线了。
        流式下响应头立刻返回，且 socket 超时对"每次读取"生效——模型超过
        timeout_s 秒没有吐新内容就会中断，而不是无限等下去。
        """
        url = self._url(path)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        t = timeout or int(self.gen.get("timeout_s", 600))

        max_try = int(self.gen.get("retries", 2)) + 1
        # 限流(429)与临时网络错误要整体重试：流式是抽取的主路径，
        # 一次 429 就把整块判失败，几十块下来整批就废了。
        for attempt in range(max_try):
            # 每次重试都重建 Request：响应体可能已被上一次尝试消费
            req = urllib.request.Request(url, data=data,
                                         headers=self._headers(), method="POST")
            text = self._stream_once(req, url, t)
            if text is not None:
                return text
            if attempt < max_try - 1:
                time.sleep(5 * (attempt + 1))
        raise LLMError(f"流式请求失败（{url}）：超过重试次数 {max_try}")

    def _stream_once(self, req, url: str, timeout_s: int) -> str | None:
        """读一次流式响应。返回 None 表示"可重试的失败"（限流/临时网络错误）。"""
        parts: list[str] = []
        reasoning_len = 0
        raw_lines: list[str] = []
        try:
            with opener_for(url).open(req, timeout=timeout_s) as r:
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    raw_lines.append(line)
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("error"):
                        raise LLMError(f"流式返回错误：{str(obj['error'])[:300]}")
                    for ch in (obj.get("choices") or []):
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            parts.append(delta["content"])
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        if rc:
                            reasoning_len += len(rc)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:400]
            except Exception:
                pass
            if e.code == 429:                      # 限流：等一会儿再来，可以重试
                return None
            raise LLMError(f"HTTP {e.code}：{body}") from e
        except urllib.error.URLError as e:         # 连接中断/超时：可以重试
            return None

        text = "".join(parts)
        if text:
            return text
        # 服务端可能根本不支持 SSE（直接回了整包 JSON）：退回解析原始响应体。
        joined = "".join(raw_lines)
        if joined.startswith("{"):
            try:
                resp = json.loads(joined)
                return resp["choices"][0]["message"]["content"] or ""
            except Exception:
                pass
        if reasoning_len:
            raise LLMError(
                f"模型只返回了思考内容（{reasoning_len} 字符）、正文为空。"
                f"这是推理型模型的典型表现：思考占满了 max_tokens 预算。"
                f"请调大 max_tokens，或换成非推理模型。")
        raise LLMError(f"流式返回为空（{url}）")

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
            "stream": bool(self.gen.get("stream", True)),
        }
        # 推理型模型（如 gemma-4）会把全部 token 预算耗在内部思考上，正文一个字
        # 都出不来。reasoning_effort="none" 可让 LM Studio 关掉思考；
        # 配空字符串则不发送该字段，兼容不支持它的服务端。
        effort = str(self.gen.get("reasoning_effort", "") or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        # 推理型云端模型（qwen3 系列等）默认会长时间"思考"，而且思考阶段服务端
        # 不推送任何 SSE 事件——客户端只能干等。实测 qwen3.8-max 面对 101 字段的
        # 抽取任务，思考超过 200 秒仍未开始输出；关掉后同样任务 17 秒完成。
        if self.gen.get("disable_thinking", True):     # 默认关：不关会长时间无响应
            payload["enable_thinking"] = False
        # 结构化输出：云端 API 支持 response_format（json_object），能显著减少
        # "模型输出带解释文字/Markdown 围栏" 引发的解析失败。部分本地服务不支持，
        # 返回 4xx 时自动去掉该字段重试一次，保证兼容性。
        if response_format:
            payload["response_format"] = response_format
        # 扩展字段（enable_thinking / response_format）不一定被所有服务端支持，
        # 遇到 4xx 就逐个剔除后重试，保证换服务商时不用改代码。
        def _send() -> str:
            while True:
                try:
                    if payload["stream"]:
                        return self._stream_post("/chat/completions", payload)
                    return self._json_content(self._post("/chat/completions", payload))
                except LLMError as e:
                    dropped = False
                    if "HTTP 4" in str(e):
                        for key in ("enable_thinking", "response_format"):
                            if key in payload:
                                payload.pop(key)
                                dropped = True
                                break
                    if not dropped:
                        raise

        return _send()

    def _json_content(self, resp: dict) -> str:
        """从非流式响应里取正文；结构异常时带上请求地址，便于定位配置错误。"""
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            # 带上实际请求地址：最常见的坑是 base_url 少了 /v1，
            # 服务端会回 "Unexpected endpoint or method"，光看响应体根本看不出原因。
            url = self._url("/chat/completions")
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
