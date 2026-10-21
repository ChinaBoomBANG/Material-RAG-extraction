"""混合检索：稠密向量 + BM25 稀疏，RRF 融合，可选重排。

为什么要混合：材料文献里的关键信息有两类。
  - 语义类："这个合金的屈服强度是多少" —— 向量检索擅长；
  - 精确类："7075"、"T6"、"wt.%"、"MPa" —— BM25 擅长，向量容易漂。
RRF（Reciprocal Rank Fusion）对两路排名做融合，无需调权重，稳健且无需训练。
"""

from __future__ import annotations

import numpy as np

from .. import config as cfg
from ..models import Chunk
from .embedder import Embedder
from .store import Store


class Retriever:
    def __init__(self, store: Store, embedder: Embedder, conf: dict | None = None):
        self.store = store
        self.embedder = embedder
        self.conf = conf or cfg.load()
        self._reranker = None

    # ---------------------------------------------------------- 检索

    def search(self, query: str, top_k: int | None = None,
               kinds: list[str] | None = None,
               doc_ids: list[str] | None = None) -> list[dict]:
        """返回 [{chunk, score, dense_rank, sparse_rank}]。"""
        r = self.conf["retrieval"]
        top_k = top_k or int(r["top_k_final"])

        dense: list[tuple[int, float]] = []
        if self.embedder.backend != "none":
            qv = self.embedder.encode_query(query)
            dense = self.store.dense_search(qv, int(r["top_k_dense"]))
        sparse = self.store.sparse_search(query, int(r["top_k_sparse"]))

        fused = self._fuse(dense, sparse, int(r["rrf_k"]))
        results = []
        for idx, score in fused:
            c = self.store.get(idx)
            if kinds and c.kind not in kinds:
                continue
            if doc_ids and c.doc_id not in doc_ids:
                continue
            d_rank = next((i for i, (j, _) in enumerate(dense) if j == idx), None)
            s_rank = next((i for i, (j, _) in enumerate(sparse) if j == idx), None)
            results.append({"chunk": c, "score": score,
                            "dense_rank": d_rank, "sparse_rank": s_rank})
            if len(results) >= top_k * 4:
                break

        if r.get("rerank", {}).get("enabled"):
            results = self._rerank(query, results)

        return results[:top_k]

    # ---------------------------------------------------------- 融合

    def _fuse(self, dense: list[tuple[int, float]],
              sparse: list[tuple[int, float]], k: int) -> list[tuple[int, float]]:
        mode = self.conf["retrieval"].get("fusion", "rrf")
        acc: dict[int, float] = {}
        if mode == "rrf":
            for rank, (idx, _) in enumerate(dense):
                acc[idx] = acc.get(idx, 0.0) + 1.0 / (k + rank + 1)
            for rank, (idx, _) in enumerate(sparse):
                acc[idx] = acc.get(idx, 0.0) + 1.0 / (k + rank + 1)
        else:  # weighted：分别归一化后加权
            w_d, w_s = 0.6, 0.4
            if dense:
                mx = max(s for _, s in dense) or 1.0
                for idx, s in dense:
                    acc[idx] = acc.get(idx, 0.0) + w_d * (s / mx)
            if sparse:
                mx = max(s for _, s in sparse) or 1.0
                for idx, s in sparse:
                    acc[idx] = acc.get(idx, 0.0) + w_s * (s / mx)
        return sorted(acc.items(), key=lambda x: -x[1])

    # ---------------------------------------------------------- 重排

    def _rerank(self, query: str, results: list[dict]) -> list[dict]:
        """交叉编码器重排。同样直连 transformers，避开 sklearn 依赖。"""
        if self._reranker is None:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            path = cfg.path("retrieval.rerank.model_path")
            self._rr_tok = AutoTokenizer.from_pretrained(path, local_files_only=True)
            self._reranker = AutoModelForSequenceClassification.from_pretrained(
                path, local_files_only=True)
            self._reranker.eval()
            self._torch = torch

        pairs = [[query, r["chunk"].text[:1024]] for r in results]
        enc = self._rr_tok(pairs, padding=True, truncation=True,
                           max_length=512, return_tensors="pt")
        with self._torch.no_grad():
            logits = self._reranker(**enc).logits.view(-1).float().numpy()
        for r, s in zip(results, logits):
            r["rerank_score"] = float(s)
        return sorted(results, key=lambda r: -r["rerank_score"])


def format_context(results: list[dict], max_chars_per_chunk: int = 1200) -> str:
    """把检索结果组织成提示词上下文，带可引用编号。"""
    lines = []
    for i, r in enumerate(results, 1):
        c: Chunk = r["chunk"]
        text = c.text[:max_chars_per_chunk]
        lines.append(
            f"[{i}] 来源：{c.doc_name} 第{c.page}页（{c.kind}，块ID {c.chunk_id}）\n{text}"
        )
    return "\n\n".join(lines)
