"""命令行入口。

    python -m rag.cli status
    python -m rag.cli ingest [--glob "data/raw_pdfs/*.pdf"] [--force]
    python -m rag.cli ask "6061 铝合金的时效工艺是什么" [-k 8]
    python -m rag.cli fill --excel "C:/path/目标表.xlsx" [--mode auto] [--max-chunks 0] [--yes]
    python -m rag.cli serve [--port 8848]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata

from . import config as cfg
from .extract.extractor import records_to_json
from .writeback import diff as diffmod


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- 终端表格渲染

def _w(s) -> int:
    """按东亚字符宽度计算显示宽度，保证中文列对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(s))


def _pad(s, n: int, right: bool = False) -> str:
    s = "" if s is None else str(s)
    gap = max(0, n - _w(s))
    return (" " * gap + s) if right else (s + " " * gap)


def format_records_text(records, specs, max_cols: int = 12) -> str:
    """把抽取结果渲染成终端表格（列名用字段中文名，不需要目标 Excel）。"""
    from .writeback.excel import ExcelTarget

    rows, _ev = ExcelTarget.to_rows(records, specs, trusted_only=True)
    if not rows:
        return "（没有抽取到通过原文核验的数据；未通过核验的字段不会显示）"

    # 列顺序跟随字段词典顺序，只保留至少有一条记录有值的列
    ordered = [s.column for s in specs.values() if any(s.column in r for r in rows)]
    hidden = max(0, len(ordered) - max_cols)
    cols = ordered[:max_cols]

    widths = [max(_w(c), max((_w(r.get(c, "")) for r in rows), default=0)) for c in cols]
    head = "  ".join(_pad(c, w) for c, w in zip(cols, widths))
    sep = "-" * _w(head)
    body = "\n".join("  ".join(_pad(r.get(c, ""), w) for c, w in zip(cols, widths))
                     for r in rows)
    out = [f"共 {len(rows)} 行 × {len(ordered)} 个有效字段", "", head, sep, body]
    if hidden:
        out.append(f"... 另有 {hidden} 列未显示，完整结果可用 --json 查看或写入 Excel")
    return "\n".join(out)


def cmd_status(args) -> int:
    from .pipeline import RagPipeline
    p = RagPipeline()
    st = p.status()
    print("== 索引 ==")
    _print(st["index"])
    print(f"原文献目录：{st['raw_dir']}")
    print(f"索引目录  ：{st['index_dir']}")
    print(f"向量后端  ：{st['embedding_backend']}")
    print("== 大模型 ==")
    _print(st["llm"])
    return 0


def cmd_ingest(args) -> int:
    from .pipeline import RagPipeline
    p = RagPipeline(lazy=False)
    r = p.ingest(pdf_glob=args.glob, force=args.force)
    print(f"索引完成：处理 {r['files']} 篇，跳过 {r['skipped']} 篇，"
          f"共 {r['chunks']} 块（含表格 {r['tables']} 个）")
    for d in r["details"]:
        print(f"  - {d['file']}: {d['status']}"
              + (f"（{d.get('pages')}页 / {d.get('chunks')}块 / 表{d.get('tables')}）"
                 if d.get("chunks") else ""))
    _print(r["index_stats"])
    return 0


def cmd_ask(args) -> int:
    from .pipeline import RagPipeline
    p = RagPipeline()
    ans = p.ask(args.question, top_k=args.top_k)
    print("\n===== 回答 =====")
    print(ans.answer)
    print("\n===== 引用来源 =====")
    for c in ans.citations:
        print(f"[{c['no']}] {c['doc']} 第{c['page']}页（{c['kind']}，score={c['score']}）")
        print(f"     {c['excerpt'][:160]}...")
    return 0


def cmd_fill(args) -> int:
    """抽取数据。默认只把结果显示在终端；指定 --excel 时才准备写表。"""
    from .pipeline import RagPipeline
    p = RagPipeline(lazy=False)
    res = p.extract(excel_path=args.excel, doc_ids=args.doc,
                    mode=args.mode, max_chunks=args.max_chunks,
                    exclude_ids=args.skip_ids,
                    schema=args.schema, workers=(args.workers or None))
    recs = res["records"]
    dropped = res.get("dropped") or []
    print(f"抽取完成：候选块 {res['picked_chunks']} 个，得到记录 {len(recs)} 条")
    sinfo = res.get("schema") or {}
    sdef = sinfo.get("schema") or {}
    if sdef:
        print(f"抽取模板：{sdef.get('name')}（{sdef.get('field_count')} 字段，"
              f"来源 {sinfo.get('mode')}）｜并发 {res.get('workers', 1)}")
    if dropped:
        print(f"已过滤 {len(dropped)} 条非样品记录（网格/统计行/无法绑定样品）：")
        for d in dropped[:8]:
            print(f"  - p{d['page']} 「{d['row_label'] or '无标签'}」：{d['reason']}")
        if len(dropped) > 8:
            print(f"  …… 其余 {len(dropped) - 8} 条省略")

    if args.excel:
        print(f"目标表：{args.excel}")
        print(f"字段映射：命中词典 {len(res['schema'].get('matched', []))} 列，"
              f"自定义列 {len(res['schema'].get('custom', []))} 列")
    else:
        print("（未指定 --excel，结果直接显示，不会修改任何文件）")

    if not recs:
        print("没有抽取到任何记录。")
        return 1

    if args.json:
        print(records_to_json(recs))

    # ---- 未指定 Excel：结果直接显示在终端 ----
    if not args.excel:
        print()
        print(format_records_text(recs, res["specs"]))
        print()
        print('如需写入表格，请加 --excel "目标.xlsx"；确认后再加 --yes 落盘。')
        return 0

    # ---- 指定了 Excel：预览 -> 写入 ----
    wb = p.writeback(recs, res["specs"], args.excel,
                     dry_run=not args.yes, trusted_only=not args.all_values)
    print(f"\n预览已生成：{wb['preview']['dir']}")
    print(f"  预览行数 {wb['rows']}，待写入单元格 {wb['cells']}")
    print(f"  证据明细 {wb['evidence_rows']} 条，其中未通过原文核验 "
          f"{wb['unverified_evidence_rows']} 条（默认不写入数据表）")
    w = wb["write"]
    if w.get("dry_run"):
        print(f"\n[预览模式] {w['note']}")
        print(f"确认无误后，请加 --yes 重新执行以写入 {args.excel}")
    else:
        print("\n===== 写入结果 =====")
        print(f"目标：{w['target']} sheet={w['sheet']}")
        print(f"备份：{w['backup']}")
        print(diffmod.format_report(w))
        print(f"\n完整报告：{wb['report_json']}")
    return 0


def _is_our_service(url: str) -> bool:
    """判断占用端口的服务是不是本项目（避免把别的程序误认成自己）。"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url}/api/config", timeout=3) as r:
            data = json.load(r)
        return bool(data.get("ok") and "llm_base_url" in data)
    except Exception:
        return False


def cmd_serve(args) -> int:
    import socket
    import threading
    import webbrowser

    import uvicorn

    from .web.app import app

    host = args.host or cfg.get("web.host", "127.0.0.1")
    port = int(args.port or cfg.get("web.port", 8848))
    url = f"http://{host}:{port}"
    auto_open = not getattr(args, "no_browser", False)

    # 端口预检：目标端口已被占用时，不要直接抛一大段 bind 错误。
    # 最常见的情况是"上一次的服务窗口还开着"，此时引导用户使用它即可。
    in_use = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            in_use = (s.connect_ex((host, port)) == 0)
    except OSError:
        in_use = False

    if in_use:
        if _is_our_service(url):
            print(f"[提示] {url} 已经有本项目的服务在运行（多半是之前的窗口还没关）。")
            print(f"       无需重复启动。")
            print(f"       若要重启，请先在原来的窗口按 Ctrl+C，或关掉它再运行本脚本。")
            if auto_open:
                print(f"       已为你打开浏览器。")
                webbrowser.open(url)
        else:
            print(f"[错误] 端口 {port} 被其他程序占用，无法启动。")
            print(f"       可换个端口：my_venv\\Scripts\\python.exe -m rag.cli serve --port 8849")
        return 0

    print(f"启动界面：{url}")
    print("停止服务：按 Ctrl+C")
    if auto_open:
        # 等服务真正起来再打开浏览器，避免出现空白页
        threading.Timer(2.0, lambda: webbrowser.open(url)).start()

    # 静默启动（pythonw）没有控制台，把 stdout/stderr 落到文件里，出问题还能查。
    log_path = getattr(args, "log_file", None)
    if log_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            fh = open(log_path, "a", encoding="utf-8", buffering=1)
            sys.stdout = fh
            sys.stderr = fh
        except OSError:
            pass

    # 写 pid 文件：静默启动（pythonw，无控制台）时没有 Ctrl+C 可按，
    # pid 文件用于识别已有实例，也便于排查/手工结束进程。
    pid_path = os.path.join(cfg.path("project.output_dir"), "web.pid")
    try:
        cfg.ensure_dir(os.path.dirname(pid_path))
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pid_path = ""
    # 用 Server 实例而不是 uvicorn.run()：页面生命周期的看门狗需要
    # 通过 app.state.uvicorn_server 把 should_exit 置 True 才能优雅停机。
    uv_config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(uv_config)
    try:
        app.state.uvicorn_server = server
    except Exception:
        pass
    try:
        server.run()
    finally:
        if pid_path and os.path.isfile(pid_path):
            try:
                os.remove(pid_path)
            except OSError:
                pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rag", description="MaterialRAG 命令行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="查看索引与模型状态").set_defaults(func=cmd_status)

    p1 = sub.add_parser("ingest", help="解析并索引 PDF")
    p1.add_argument("--glob", default=None, help="PDF 通配路径，默认取 data/raw_pdfs/*.pdf")
    p1.add_argument("--force", action="store_true", help="已索引的文献也重新解析")
    p1.set_defaults(func=cmd_ingest)

    p2 = sub.add_parser("ask", help="基于文献问答")
    p2.add_argument("question")
    p2.add_argument("-k", "--top-k", type=int, default=None)
    p2.set_defaults(func=cmd_ask)

    p3 = sub.add_parser("fill", help="抽取数据（默认只显示在终端；加 --excel 才写表）")
    p3.add_argument("--excel", default=None,
                    help="目标 Excel 路径。不填则结果直接显示在终端，不修改任何文件")
    p3.add_argument("--mode", default="auto", choices=["auto", "table", "all"],
                    help="抽取范围：table=只抽表格（最快）；"
                         "auto=表格+正文里出现「数值+单位」的段落（默认，比 table 多几倍）；"
                         "all=全部块（最慢）")
    p3.add_argument("--max-chunks", type=int, default=0,
                    help="最多处理多少块（0=不限）。先试 3 块看质量再放开")
    p3.add_argument("--doc", nargs="*", default=None, help="限定文献 ID（文件名去扩展名）")
    p3.add_argument("--skip-ids", nargs="*", default=None,
                    help="续抽：已抽过的块 id 列表（界面会自动带上；命令行一般不必手动填）")
    p3.add_argument("--schema", default=None,
                    help="抽取模板：模板名（如 material_default）/ YAML 文件路径。"
                         "指定了 --excel 时以表头为准，此参数被覆盖")
    p3.add_argument("--workers", type=int, default=0, help="并发请求数（0=取 config 的 extract.workers）")
    p3.add_argument("--yes", action="store_true", help="确认写入（不加则只出预览）")
    p3.add_argument("--all-values", action="store_true",
                    help="连未通过原文核验的值也写入（默认不写）")
    p3.add_argument("--json", action="store_true", help="同时打印记录 JSON")
    p3.set_defaults(func=cmd_fill)

    p4 = sub.add_parser("serve", help="启动 Web 界面")
    p4.add_argument("--host", default=None)
    p4.add_argument("--port", type=int, default=None)
    p4.add_argument("--no-browser", action="store_true",
                    help="不自动打开浏览器（默认会自动打开）")
    p4.add_argument("--log-file", default=None,
                    help="把日志写入文件（静默启动用 pythonw 时没有控制台，靠这个留痕）")
    p4.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
