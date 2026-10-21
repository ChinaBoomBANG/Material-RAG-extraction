"""前端构建与部署自动化（Windows 友好，纯 Python，不依赖 shell 特性）。

做三件事：
  1. 找到 node / npm（优先 PATH，其次 WorkBuddy 托管目录）；
  2. 需要时 npm install（node_modules 缺失或 package.json 更新）；
  3. npm run build 并把产物落到 rag/web/dist，最后自检 index.html 与 assets 是否齐全。

用法:
    python scripts/build_web.py            # 安装（按需）+ 构建 + 自检
    python scripts/build_web.py --check    # 只看当前产物状态，不构建
    python scripts/build_web.py --clean    # 删 node_modules 与 dist 后重建
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
DIST = os.path.join(ROOT, "rag", "web", "dist")

NODE_FALLBACKS = [
    r"C:\Users\25585\.workbuddy\binaries\node\versions\22.22.2-6",
    r"C:\Users\25585\.workbuddy\binaries\node\versions\22.22.2-3",
    r"D:\yingyong\nodejs",
]

COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def _run_npm(npm: str, args: list[str], cwd: str, quiet: bool = False):
    """npm 在 Windows 上是 .cmd，必须经 cmd.exe 调用，否则 CreateProcess 会失败。

    输出编码不一定是 UTF-8（中文系统下 npm 会吐 GBK），所以按字节读再自行解码。
    """
    # 必须把 npm 路径作为独立参数交给 cmd /c，让 Python 负责加引号；
    # 拼成一整条带内嵌引号的字符串时 cmd 会解析失败（实测踩过）。
    full = [COMSPEC, "/c", npm] + args
    if quiet:
        r = subprocess.run(full, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return r.returncode, _decode(r.stdout)
    return subprocess.call(full, cwd=cwd), ""


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _npm_candidates() -> list[str]:
    """候选顺序：已知可用的托管目录 > PATH > 其他目录。

    本机 22.22.2-3 目录里的 npm 是残缺的（报 Cannot find module
    '../../package.json'），而 shutil.which 恰好先命中它，所以不能只信 PATH。
    """
    cands: list[str] = []
    for d in NODE_FALLBACKS:
        for name in ("npm.cmd", "npm.CMD", "npm"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                cands.append(p)
    for name in ("npm.cmd", "npm.CMD", "npm"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    # 去重且保序
    seen, out = set(), []
    for c in cands:
        k = os.path.normcase(c)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _pick_npm() -> tuple[str | None, str]:
    """逐个探测：能正常输出 --version 的才用（本机存在残缺的 node 目录）。"""
    for c in _npm_candidates():
        try:
            code, out = _run_npm(c, ["--version"], cwd=WEB, quiet=True)
        except OSError:
            continue
        ver = (out or "").strip().splitlines()[-1] if out else ""
        if code == 0 and ver and ver[0].isdigit():
            return c, ver
    return None, ""


def status() -> dict:
    idx = os.path.join(DIST, "index.html")
    assets = os.path.join(DIST, "assets")
    js = [f for f in os.listdir(assets)] if os.path.isdir(assets) else []
    return {
        "dist": DIST,
        "built": os.path.isfile(idx),
        "index_size": os.path.getsize(idx) if os.path.isfile(idx) else 0,
        "assets": len(js),
        "asset_files": sorted(js),
        "dist_mtime": int(os.path.getmtime(idx)) if os.path.isfile(idx) else 0,
    }


def need_install() -> bool:
    nm = os.path.join(WEB, "node_modules")
    pkg = os.path.join(WEB, "package.json")
    lock = os.path.join(WEB, "package-lock.json")
    if not os.path.isdir(nm):
        return True
    newest_dep = max(os.path.getmtime(p) for p in (pkg, lock) if os.path.isfile(p))
    marker = os.path.join(nm, ".package-lock.json")
    stamp = os.path.getmtime(marker) if os.path.isfile(marker) else 0
    return newest_dep > stamp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告产物状态")
    ap.add_argument("--clean", action="store_true", help="先删 node_modules 与 dist")
    ap.add_argument("--force-install", action="store_true", help="强制 npm install")
    args = ap.parse_args()

    if args.check:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0

    if not os.path.isdir(WEB):
        print(f"[错误] 找不到前端目录：{WEB}")
        return 1

    npm, npm_ver = _pick_npm()
    node = shutil.which("node") or shutil.which("node.exe")
    if not npm:
        print("[错误] 找不到可用的 npm。安装 Node.js 18+ 后重试，或把安装目录加进 PATH。")
        return 1
    print(f"[1/4] npm  = {npm}（{npm_ver}）")
    print(f"      node = {node or '（未在 PATH，npm 自带环境可继续）'}")

    if args.clean:
        for d in (os.path.join(WEB, "node_modules"), DIST):
            if os.path.isdir(d):
                print(f"      清理 {d}")
                shutil.rmtree(d, ignore_errors=True)

    t0 = time.time()
    if args.force_install or need_install():
        print("[2/4] 安装依赖（npm install）…")
        code, out = _run_npm(npm, ["install", "--no-audit", "--no-fund"], cwd=WEB)
        if code != 0:
            print("[错误] npm install 失败")
            return 1
    else:
        print("[2/4] 依赖已是最新，跳过安装")

    print("[3/4] 构建（npm run build）…")
    code, out = _run_npm(npm, ["run", "build"], cwd=WEB)
    if code != 0:
        print("[错误] 构建失败")
        return 1

    st = status()
    print("[4/4] 产物自检")
    print(f"      dist        = {st['dist']}")
    print(f"      index.html  = {'有' if st['built'] else '缺失'}（{st['index_size']} 字节）")
    print(f"      assets      = {st['assets']} 个文件")
    for f in st["asset_files"][:8]:
        print(f"        - {f}")
    ok = st["built"] and st["assets"] > 0 and st["index_size"] > 200
    print(f"\n{'构建完成' if ok else '构建产物不完整'}，用时 {time.time() - t0:.1f}s")
    if ok:
        print("重启服务后访问 http://127.0.0.1:8848/ 即为新界面")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
