"""本地向量库与 BM25 索引。

选型说明：文献规模在数百到数千篇、块数万级时，numpy 精确内积检索
（矩阵乘法 + argpartition）在 CPU 上已是毫秒级，无需引入 Chroma / FAISS
等外部依赖。这样整套系统只依赖 numpy，部署与迁移成本最低。
索引以 .npz + json 落盘，可随时重建。
"""

from __future__ import annotations

import json
import os
import pickle
import re
from typing import Any

import numpy as np

from ..models import Chunk

# ---------------------------------------------------------------- 分词

_TOKEN = re.compile(r"[A-Za-z][A-Za-z\-]*|\d+(?:\.\d+)?%?|[\u4e00-\u9fff]")
# 材料领域停用词（保留成分与工艺关键词）
_STOP = {"的", "了", "和", "与", "及", "或", "在", "是", "为", "对", "中", "上",
         "下", "the", "a", "an", "of", "and", "or", "in", "on", "for", "to", "is"}


def tokenize(text: str) -> list[str]:
    """中英混合分词：英文按词、数字带单位、中文按字。

    中文按字切分对 BM25 已足够，因为材料术语多为 2-4 字，且
    向量检索负责语义部分，关键词通道只需保证术语可命中。
    """
    toks = [t.lower() for t in _TOKEN.findall(text)]
    return [t for t in toks if t not in _STOP]


# ---------------------------------------------------------------- 主体

class Store:
    """块 + 向量的持久化存储，附带 BM25 稀疏检索。"""

    def __init__(self, index_dir: str):
        self.index_dir = index_dir
        os.makedirs(index_dir, exist_ok=True)
        self.chunks: list[Chunk] = []
        self.metas: dict[str, dict] = {}
        self.vectors: np.ndarray | None = None
        self._bm25 = None

    # ---------------- 路径 ----------------
    @property
    def _p_chunks(self) -> str:
        return os.path.join(self.index_dir, "chunks.jsonl")

    @property
    def _p_meta(self) -> str:
        return os.path.join(self.index_dir, "docs.json")

    @property
    def _p_vec(self) -> str:
        return os.path.join(self.index_dir, "vectors.npy")

    @property
    def _p_bm25(self) -> str:
        return os.path.join(self.index_dir, "bm25.pkl")

    # ---------------- 写入 ----------------
    def add(self, chunks: list[Chunk], vectors: np.ndarray | None) -> None:
        # 不许出现「块加了、向量没加」的状态：两者的下标必须一一对应，
        # 否则 dense_search 拿到的下标会指向错误的块（静默返回别人家的数据）。
        if self.vectors is not None and vectors is None:
            raise ValueError(
                "索引里已有向量，本次却没有提供向量（chunks 与 vectors 会错位）。"
                "请检查 embedding.backend 是否被改动；确实要转纯 BM25 请先重建索引。")
        if self.vectors is None:
            self.vectors = vectors
        elif vectors is not None:
            self.vectors = np.vstack([self.vectors, vectors])
        self.chunks.extend(chunks)

    def set_meta(self, doc_id: str, meta: dict) -> None:
        self.metas[doc_id] = meta

    def has_doc(self, doc_id: str) -> bool:
        return doc_id in self.metas

    def remove_doc(self, doc_id: str) -> None:
        """移除某篇文献的块与向量（用于重新索引）。"""
        if doc_id not in self.metas:
            return
        keep = [i for i, c in enumerate(self.chunks) if c.doc_id != doc_id]
        self.chunks = [self.chunks[i] for i in keep]
        if self.vectors is not None and len(keep):
            self.vectors = self.vectors[keep]
        elif self.vectors is not None:
            self.vectors = None
        self.metas.pop(doc_id, None)
        self._bm25 = None

    def build_sparse(self) -> None:
        from rank_bm25 import BM25Okapi
        corpus = [tokenize(c.text) for c in self.chunks]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def save(self) -> None:
        with open(self._p_chunks, "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        with open(self._p_meta, "w", encoding="utf-8") as f:
            json.dump(self.metas, f, ensure_ascii=False, indent=2)
        if self.vectors is not None:
            np.save(self._p_vec, self.vectors)
        elif os.path.exists(self._p_vec):
            os.remove(self._p_vec)
        self.build_sparse()
        with open(self._p_bm25, "wb") as f:
            pickle.dump(self._bm25, f)

    # ---------------- 读取 ----------------
    def load(self) -> "Store":
        if not os.path.exists(self._p_chunks):
            return self
        self.chunks = []
        with open(self._p_chunks, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                d["bbox"] = tuple(d["bbox"]) if d.get("bbox") else None
                self.chunks.append(Chunk(**d))
        if os.path.exists(self._p_meta):
            with open(self._p_meta, "r", encoding="utf-8") as f:
                self.metas = json.load(f)
        if os.path.exists(self._p_vec):
            self.vectors = np.load(self._p_vec)
        if os.path.exists(self._p_bm25):
            with open(self._p_bm25, "rb") as f:
                self._bm25 = pickle.load(f)
        else:
            self.build_sparse()
        return self

    # ---------------- 检索 ----------------
    def dense_search(self, qvec: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self.vectors is None or len(self.chunks) == 0:
            return []
        q = qvec.astype(np.float32)
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        sims = self.vectors @ q
        k = min(top_k, len(sims))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(i), float(sims[i])) for i in idx]

    def sparse_search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        if self._bm25 is None:
            self.build_sparse()
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(tokenize(query))
        k = min(top_k, len(scores))
        if k <= 0:
            return []
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        return [(int(i), float(scores[i])) for i in idx]

    def get(self, idx: int) -> Chunk:
        return self.chunks[idx]

    def stats(self) -> dict[str, Any]:
        docs = {c.doc_id for c in self.chunks}
        return {
            "docs": len(docs),
            "chunks": len(self.chunks),
            "tables": sum(1 for c in self.chunks if c.kind == "table"),
            "vectors": 0 if self.vectors is None else int(self.vectors.shape[0]),
            "dim": 0 if self.vectors is None else int(self.vectors.shape[1]),
        }
