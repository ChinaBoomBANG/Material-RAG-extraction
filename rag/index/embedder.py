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
        base = llm["base_url"].rstrip("/")
        model = self.conf["embedding"].get("lmstudio_model") or self._pick_embedding_model(base)
        body = json.dumps({"model": model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/embeddings", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {llm.get('api_key', 'lm-studio')}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
        except urllib.error.URLError as e:
            raise RuntimeError(f"LM Studio embedding 调用失败：{e}") from e
        vecs = np.asarray([d["embedding"] for d in data["data"]], dtype=np.float32)
        self.dim = vecs.shape[1]
        return vecs

    def _pick_embedding_model(self, base: str) -> str:
        with urllib.request.urlopen(f"{base}/models", timeout=15) as r:
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
