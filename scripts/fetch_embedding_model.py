"""下载可离线运行的 embedding 模型（默认 BAAI/bge-small-zh-v1.5）。

背景：hf-mirror 不支持 huggingface_hub 新版默认启用的 Xet 传输协议，
会返回 401。因此这里绕开 huggingface_hub，直接按文件清单走 HTTPS 直链下载。

用法：
    python scripts/fetch_embedding_model.py
    python scripts/fetch_embedding_model.py --repo BAAI/bge-m3 --dest data/models/bge-m3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

DEFAULT_REPO = "BAAI/bge-small-zh-v1.5"
DEFAULT_MIRROR = "https://hf-mirror.com"
# hf-mirror 会拦截 urllib 的默认 User-Agent（403），必须伪装浏览器 UA
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 只取推理必需的权重文件，跳过 pytorch_model.bin（与 safetensors 等价但体积翻倍）
REQUIRED = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "1_Pooling/config.json",
    "model.safetensors",
]


def list_files(repo: str, mirror: str) -> list[str]:
    url = f"{mirror}/api/models/{repo}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return [s["rfilename"] for s in data.get("siblings", [])]


def download(repo: str, filename: str, dest_root: str, mirror: str) -> None:
    dest = os.path.join(dest_root, filename)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  [跳过] {filename} 已存在 ({os.path.getsize(dest) / 2**20:.1f} MB)")
        return
    url = f"{mirror}/{repo}/resolve/main/{filename}"
    tmp = dest + ".part"
    print(f"  [下载] {filename}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            buf = r.read(1 << 20)
            if not buf:
                break
            f.write(buf)
    os.replace(tmp, dest)
    print(f"         -> {os.path.getsize(dest) / 2**20:.1f} MB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--dest", default="data/models/bge-small-zh-v1.5")
    ap.add_argument("--mirror", default=DEFAULT_MIRROR)
    args = ap.parse_args()

    available = list_files(args.repo, args.mirror)
    wanted = [f for f in REQUIRED if f in available]
    missing = [f for f in REQUIRED if f not in available]
    if missing:
        print(f"[警告] 仓库中不存在以下文件，已跳过：{missing}")

    print(f"[信息] 仓库 {args.repo} -> {args.dest}")
    for f in wanted:
        download(args.repo, f, args.dest, args.mirror)

    print("[完成] embedding 模型就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
