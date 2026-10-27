"""向量化：本机 transformers 直连，或 LM Studio 的 /v1/embeddings。

为什么不用 sentence-transformers：
    它间接依赖 scikit-learn，而 sklearn 在部分 Windows 环境下缺少
    vcomp140.dll（VC++ 运行库）会直接导入失败。本模块只用到"分词 + 前向 +
    池化 + 归一化"四步，用 transformers 直接实现即可，依赖面更小、更可控。

池化方式按模型自带的 1_Pooling/config.json 决定（bge-zh 系列为 CLS 池化）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import numpy as np

from .. import config as cfg


class Embedder:
    """统一的向量化接口。"""

    def __init__(self, conf: dict | None = None):
        self.conf = conf or cfg.load()
        self.backend = self.conf["embedding"]["backend"]
        self.dim = 0
        self._tok = None
        self._model = None
        self._pooling = "cls"

    # ---------------------------------------------------------- 初始化

    def load(self) -> None:
        if self.backend == "none":
            return
        if self.backend == "local":
            self._load_local()
        elif self.backend == "lmstudio":
            self.dim = 0
        else:
            raise ValueError(f"未知的 embedding.backend: {self.backend}")

    def _load_local(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        model_path = cfg.path("embedding.model_path")
        if not _looks_complete(model_path):
            raise FileNotFoundError(
                f"本地 embedding 模型不完整：{model_path}\n"
                f"请先执行：python scripts/fetch_embedding_model.py")

        self._tok = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self._model = AutoModel.from_pretrained(model_path, local_files_only=True)
        self._model.eval()
        self._torch = torch
        self._pooling = _read_pooling(model_path)
        self.dim = int(self._model.config.hidden_size)

    # ---------------------------------------------------------- 编码

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        # 部分模型（如 nomic-embed-text）要求文档侧带任务前缀才能发挥最佳效果；
        # 前缀为空时行为与之前完全一致。
        prefix = self.conf["embedding"].get("doc_prefix", "") or ""
        if prefix:
            texts = [prefix + t for t in texts]
        return self._encode(texts)

    def encode_query(self, text: str) -> np.ndarray:
        prefix = self.conf["embedding"].get("query_prefix", "") or ""
        return self._encode([prefix + text])[0]

    def _encode(self, texts: list[str]) -> np.ndarray:
        if self.backend == "none":
            raise RuntimeError("embedding.backend 为 none，无法编码")
        if self.backend == "lmstudio":
            return self._encode_lmstudio(texts)
        if self._model is None:
            self.load()

        torch = self._torch
        bs = int(self.conf["embedding"]["batch_size"])
        normalize = bool(self.conf["embedding"].get("normalize", True))
        chunks: list[np.ndarray] = []

        for i in range(0, len(texts), bs):
            batch = [t if t.strip() else " " for t in texts[i:i + bs]]
            enc = self._tok(batch, padding=True, truncation=True,
                            max_length=512, return_tensors="pt")
            with torch.no_grad():
                out = self._model(**enc)
            last = out.last_hidden_state
            if self._pooling == "mean":
                mask = enc["attention_mask"].unsqueeze(-1).float()
                emb = (last * mask).sum(1) / mask.sum(1).clamp(min=1e-6)
            else:                                   # cls
                emb = last[:, 0]
            if normalize:
                emb = torch.nn.functional.normalize(emb, p=2, dim=1)
            chunks.append(emb.cpu().numpy().astype(np.float32))

        return np.vstack(chunks) if chunks else np.zeros((0, self.dim), dtype=np.float32)

    def _encode_lmstudio(self, texts: list[str]) -> np.ndarray:
        llm = self.conf["llm"]
        emb = self.conf["embedding"]
        # embedding 有自己的地址与密钥：留空时沿用 llm 的（向后兼容）。
        # 这样把 llm.base_url 改成云端 API 时，向量化仍然留在本机，不会跟着上云。
        base = str(emb.get("base_url") or llm["base_url"]).rstrip("/")
        api_key = emb.get("api_key") or llm.get("api_key", "lm-studio")
        model = emb.get("lmstudio_model") or self._pick_embedding_model(base)
        timeout = int(emb.get("timeout_s", 120) or 120)
        # 必须分批：整库上万块一次性发送会产生十几 MB 的请求体，
        # 服务端要么超时要么直接拒收。
        bs = max(1, int(emb.get("batch_size", 32) or 32))

        parts: list[np.ndarray] = []
        total = len(texts)
        # 本地地址必须绕过系统代理，否则模型服务未启动时请求会在代理里干等到超时。
        from ..llm.client import opener_for
        url = f"{base}/embeddings"
        opener = opener_for(url)
        for i in range(0, total, bs):
            batch = texts[i:i + bs]
            body = json.dumps({"model": model, "input": batch}).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                method="POST")
            try:
                with opener.open(req, timeout=timeout) as r:
                    data = json.load(r)
            except urllib.error.URLError as e:
                raise RuntimeError(
                    f"embedding 服务调用失败（{base}，模型 {model}，"
                    f"第 {i + 1}~{i + len(batch)} 条）：{e}") from e
            parts.append(np.asarray([d["embedding"] for d in data["data"]],
                                    dtype=np.float32))
            if total > bs:
                print(f"  embedding {min(i + bs, total)}/{total}", flush=True)

        vecs = np.vstack(parts) if parts else np.zeros((0, self.dim), dtype=np.float32)
        # 必须归一化：dense_search 只归一化查询向量、直接做内积，
        # 服务端若不返回单位向量，点积就不再等于余弦，排序会被模长带偏。
        if bool(emb.get("normalize", True)):
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.clip(norms, 1e-12, None)
        self.dim = vecs.shape[1] if len(vecs) else self.dim
        return vecs

    def _pick_embedding_model(self, base: str) -> str:
        from ..llm.client import opener_for
        url = f"{base}/models"
        with opener_for(url).open(url, timeout=15) as r:
            data = json.load(r)
        ids = [m["id"] for m in data.get("data", [])]
        prefer = [i for i in ids if "embed" in i.lower() or "bge" in i.lower()]
        if not prefer:
            raise RuntimeError(
                f"LM Studio 中未发现 embedding 模型，可用模型：{ids}。"
                f"请在 config 中显式设置 embedding.lmstudio_model。")
        return prefer[0]


# ---------------------------------------------------------------- 工具

def _read_pooling(model_path: str) -> str:
    p = os.path.join(model_path, "1_Pooling", "config.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("pooling_mode_cls_token"):
            return "cls"
        if d.get("pooling_mode_mean_tokens"):
            return "mean"
    except Exception:
        pass
    return "cls"


def _looks_complete(model_path: str) -> bool:
    need = ["config.json", "tokenizer.json"]
    if not all(os.path.exists(os.path.join(model_path, n)) for n in need):
        return False
    return (os.path.exists(os.path.join(model_path, "model.safetensors"))
            or os.path.exists(os.path.join(model_path, "pytorch_model.bin")))
