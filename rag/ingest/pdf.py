"""PDF 解析与版面感知切块。

学术 PDF 的两类关键内容：
  1. 正文段落——多栏排版，直接按 y 坐标排序会串行，必须按"行带"还原阅读顺序；
  2. 表格——成分表 / 工艺参数表 / 力学性能表，是结构化抽取的主要来源。

阅读顺序算法（行带法）：
  - 先用跨栏块（宽度 > 页宽 70%，如通栏标题）把页面切成纵向段；
  - 每段内把 y 区间重叠 > 50% 的块聚成一条"行带"，带内按 x 排序；
  - 该算法对单栏与双栏页面同时成立，不依赖栏数硬判定。

表格抽取（三线表算法）：
  期刊表格绝大多数是"三线表"——只有上/中/下三条横线，数据行之间没有竖线，
  也常常没有竖线。PyMuPDF 的 find_tables() 默认按线段围合单元格，在这种表上
  一个都找不到；改用 text 策略又会把整页正文误切成一格一格的"表格"。
  因此这里自己实现：
    1. 收集水平线段（含高度≈0 的填充矩形），按 y 聚类成"线带"；
    2. x 范围重叠、间距合理的连续线带 = 一张表，首末线带给出表区间；
    3. 区间内部按词的 y 中心聚类还原行（数据行之间没有线，只能靠基线）；
    4. 按所有词 x 区间的投影空隙切列（列间隙通常 >10pt，词间距只有 1~3pt）；
    5. 第 1、2 条横线之间的行视作表头（可多行，按列拼接）。
表格单独成块；表格区域内的正文块会被丢弃，避免同一内容重复入库。
"""

from __future__ import annotations

import os
import re

import pymupdf as fitz

from ..models import Chunk

HEADER_FOOTER_RATIO = 0.06     # 上下 6% 视为页眉页脚
WIDE_BLOCK_RATIO = 0.70        # 超过页宽 70% 视为跨栏块
BAND_Y_OVERLAP = 0.50          # y 重叠超过 50% 归为同一行带

RULE_MIN_LEN_RATIO = 0.12      # 横线长度下限（占页宽比例），滤掉下划线/装饰线
RULE_Y_TOL = 2.0               # 同一横线的 y 容差（pt）
TABLE_MAX_GAP = 420.0          # 组成同一张表的相邻横线最大间距（pt）
TABLE_MIN_WIDTH_RATIO = 0.20   # 表宽下限（占页宽比例）
TABLE_MAX_HEIGHT = 520.0       # 表高上限（pt），超过说明把整页图形当成了表
ROW_Y_TOL = 3.2                # 同一行词的 y 容差（pt）
COL_GAP = 4.5                  # 列间隙阈值（pt）：词间距小于此值视为同一列
MAX_COLS = 16                  # 列数上限，超过视为噪声
PROSE_ROW_CHARS = 160          # 单行字符数超过此值视为正文混入，其后全部截断
BOX_MIN_LEN = 8.0              # 描边单元格矩形的宽度下限（pt）

_REF_HEADING = re.compile(r"^\s*(references|bibliography|参考文献|致谢|acknowledg)", re.I)
_CAPTION_RE = re.compile(r"^\s*(?:(?:Table|TABLE|Tab\.?)\s*[0-9IVXivx]+|表\s*[0-9]+)")


# ---------------------------------------------------------------- 阅读顺序

def _clearly_two_columns(left: list[dict], right: list[dict], pw: float) -> bool:
    """判定一组块是否呈左右两栏：右栏左缘明显越过左栏右缘，中间有空隙。"""
    if len(left) < 2 or len(right) < 2:
        return False
    left_right_edge = max(b["bbox"][2] for b in left)
    right_left_edge = min(b["bbox"][0] for b in right)
    return (right_left_edge - left_right_edge) > pw * 0.02


def _order_within_column_free(seg: list[dict], pw: float) -> list[dict]:
    """段内排序：双栏 => 先左栏从上到下再右栏；单栏 => 行带法。

    上一版的行带法在"整页都是左右两栏、且无跨栏标题"时，会把左栏下段与
    右栏上段（y 坐标重叠）聚成一条带，导致阅读顺序错乱。这里先判栏再排序。
    """
    if not seg:
        return []
    mid = pw / 2
    left = sorted((b for b in seg if (b["bbox"][0] + b["bbox"][2]) / 2 < mid),
                  key=lambda b: b["bbox"][1])
    right = sorted((b for b in seg if (b["bbox"][0] + b["bbox"][2]) / 2 >= mid),
                   key=lambda b: b["bbox"][1])
    if _clearly_two_columns(left, right, pw):
        return left + right

    # 单栏 / 混合：行带法
    seg = sorted(seg, key=lambda b: b["bbox"][1])
    bands: list[list[dict]] = []
    for b in seg:
        for band in bands:
            ytop = min(x["bbox"][1] for x in band)
            ybot = max(x["bbox"][3] for x in band)
            ov = min(ybot, b["bbox"][3]) - max(ytop, b["bbox"][1])
            h = min(ybot - ytop, b["bbox"][3] - b["bbox"][1])
            if h > 0 and ov / h > BAND_Y_OVERLAP:
                band.append(b)
                break
        else:
            bands.append([b])
    out: list[dict] = []
    for band in bands:
        band.sort(key=lambda b: b["bbox"][0])
        out.extend(band)
    return out


def _reading_order(blocks: list[dict], page_width: float, page_height: float) -> list[dict]:
    """还原阅读顺序：跨栏块把页面切成纵向段，段内再判单栏/双栏。"""
    if not blocks:
        return []
    wide = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) > page_width * WIDE_BLOCK_RATIO]
    narrow = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) <= page_width * WIDE_BLOCK_RATIO]
    wide.sort(key=lambda b: b["bbox"][1])

    segments: list[tuple[float, float, dict | None]] = []
    prev_y = 0.0
    for w in wide:
        if w["bbox"][1] > prev_y:
            segments.append((prev_y, w["bbox"][1], None))
        segments.append((w["bbox"][1], w["bbox"][3], w))
        prev_y = w["bbox"][3]
    if prev_y < page_height:
        segments.append((prev_y, page_height, None))

    out: list[dict] = []
    for y0, y1, w in segments:
        if w is not None:
            out.append(w)
            continue
        seg = [b for b in narrow if y0 - 0.5 <= b["bbox"][1] < y1 - 0.5]
        out.extend(_order_within_column_free(seg, page_width))
    return out


def _collect_blocks(page: "fitz.Page", drop_refs: bool) -> list[dict]:
    ph = page.rect.height
    blocks: list[dict] = []
    for b in page.get_text("blocks"):
        x0, y0, x1, y1, text, _no, btype = b[0], b[1], b[2], b[3], b[4], b[5], b[6]
        if btype != 0:
            continue
        text = (text or "").strip()
        if not text:
            continue
        if y1 < ph * HEADER_FOOTER_RATIO or y0 > ph * (1 - HEADER_FOOTER_RATIO):
            continue                                   # 页眉页脚
        if text.isdigit() and len(text) <= 3:
            continue                                   # 孤立页码
        blocks.append({"bbox": (x0, y0, x1, y1), "text": text})

    if drop_refs:
        for i, b in enumerate(blocks):
            if _REF_HEADING.match(b["text"]) and len(b["text"]) < 60:
                blocks = blocks[:i]
                break
    return blocks


# ---------------------------------------------------------------- 表格

def _h_rules(page: "fitz.Page") -> list[tuple[float, float, float]]:
    """收集水平线段 -> [(y, x0, x1)]，已按 y 排序。"""
    min_len = page.rect.width * RULE_MIN_LEN_RATIO
    out: list[tuple[float, float, float]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for d in drawings:
        for it in d["items"]:
            if it[0] == "l":                              # 直线段
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) <= 1.0 and abs(p1.x - p2.x) >= min_len:
                    out.append(((p1.y + p2.y) / 2.0, min(p1.x, p2.x), max(p1.x, p2.x)))
            elif it[0] == "re":                           # 矩形
                r = it[1]
                if r.height <= 1.5:                       # 细矩形 = 横线
                    if r.width >= min_len:
                        out.append(((r.y0 + r.y1) / 2.0, r.x0, r.x1))
                elif r.width >= BOX_MIN_LEN:
                    # 描边单元格（满格线框表）：上下边本身也是横线。
                    # 这里不能用横线的长度门槛——单个单元格往往很窄。
                    out.append((r.y0, r.x0, r.x1))
                    out.append((r.y1, r.x0, r.x1))
    out.sort()
    return out


def _rule_bands(rules: list[tuple[float, float, float]]) -> list[dict]:
    """y 相近的横线合并为一条线带（同一条规则线可能由多段拼成）。"""
    bands: list[dict] = []
    for y, x0, x1 in rules:
        if bands and y - bands[-1]["y"] <= RULE_Y_TOL:
            b = bands[-1]
            b["x0"], b["x1"] = min(b["x0"], x0), max(b["x1"], x1)
            b["n"] += 1
        else:
            bands.append({"y": y, "x0": x0, "x1": x1, "n": 1})
    return bands


def _caption_ys(page: "fitz.Page", y_min: float, y_max: float) -> list[float]:
    """区间内所有表题的 y0（用于把上下相邻、线框同宽的两张表拆开）。"""
    out: list[float] = []
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return out
    for b in blocks:
        if b[6] != 0:
            continue
        text = (b[4] or "").strip()
        if not text or len(text) > 300 or not _CAPTION_RE.match(text):
            continue
        if y_min < b[1] < y_max:
            out.append(b[1])
    return out


def _table_regions(page: "fitz.Page") -> list[dict]:
    """由横线带圈定表区间 -> [{"rect": (x0,y0,x1,y1), "rules": [y...]}]。

    分组判据：
      - y 间距不超过 TABLE_MAX_GAP；
      - 线带若够宽（≥50% 组宽且中心不偏），直接归入；
      - 线带若很窄但完全落在组 x 范围内且紧贴上方线带（≤80pt），视为表内的
        分段中线（三线表的中线常只画在部分列下，长度远小于表宽）；
      - 其余视为无关线条另起一组（否则图内短线会把正文段落吞成表格行）。
    只用"x 区间重叠"会把图里的短线并进表里。
    """
    groups: list[dict] = []
    for b in _rule_bands(_h_rules(page)):
        if groups:
            g = groups[-1]
            gw = g["x1"] - g["x0"]
            bw = b["x1"] - b["x0"]
            g_center = (g["x0"] + g["x1"]) / 2.0
            b_center = (b["x0"] + b["x1"]) / 2.0
            gap = b["y"] - g["ys"][-1]
            wide = bw >= gw * 0.50 and abs(b_center - g_center) <= gw * 0.25
            inner = (b["x0"] >= g["x0"] - 8 and b["x1"] <= g["x1"] + 8
                     and gap <= 80)
            if gap <= TABLE_MAX_GAP and (wide or inner):
                g["ys"].append(b["y"])
                g["x0"], g["x1"] = min(g["x0"], b["x0"]), max(g["x1"], b["x1"])
                continue
        groups.append({"ys": [b["y"]], "x0": b["x0"], "x1": b["x1"]})

    pw = page.rect.width
    regions: list[dict] = []
    for g in groups:
        # 上下相邻、线框同宽的两张表会被并进一组；夹在中间的 "Table N" 表题
        # 就是天然分隔符，在这里切开。
        caps = _caption_ys(page, g["ys"][0], g["ys"][-1])
        pieces: list[list[float]] = [[g["ys"][0]]]
        for prev, cur in zip(g["ys"], g["ys"][1:]):
            if any(prev < c < cur for c in caps):
                pieces.append([cur])
            else:
                pieces[-1].append(cur)

        for ys in pieces:
            if len(ys) < 2:
                continue
            width = g["x1"] - g["x0"]
            height = ys[-1] - ys[0]
            if (width < pw * TABLE_MIN_WIDTH_RATIO
                    or height < 8 or height > TABLE_MAX_HEIGHT):
                continue
            regions.append({
                "rect": (g["x0"], ys[0] - 1.5, g["x1"], ys[-1] + 1.5),
                "rules": list(ys),
            })
    return regions


def _rows_from_words(words: list) -> list[dict]:
    """按 y 中心聚类还原行（三线表的数据行之间没有线条，只能靠基线）。"""
    rows: list[dict] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2.0):
        yc = (w[1] + w[3]) / 2.0
        if rows and abs(yc - rows[-1]["mean"]) <= ROW_Y_TOL:
            r = rows[-1]
            r["mean"] = (r["mean"] * len(r["w"]) + yc) / (len(r["w"]) + 1)
            r["w"].append(w)
        else:
            rows.append({"mean": yc, "w": [w]})
    return rows


def _col_spans(words: list) -> list[list[float]]:
    """按 x 区间投影切列：词间距 < COL_GAP 视为同列，空隙即列边界。"""
    spans: list[list[float]] = []
    for a, b in sorted((w[0], w[2]) for w in words):
        if spans and a - spans[-1][1] < COL_GAP:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def _caption(page: "fitz.Page", rect: tuple) -> str:
    """取表区间正上方的表题（形如 "Table 3 ..." / "表 2 ..."）。"""
    _x0, y0, _x1, _y1 = rect
    caption = ""
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return ""
    for b in blocks:
        if b[6] != 0:
            continue
        by0, by1, text = b[1], b[3], (b[4] or "").strip()
        if not text or by1 > y0 + 4 or by0 < y0 - 70:
            continue
        if _CAPTION_RE.match(text):
            caption = " ".join(text.split())
    return caption[:300]


def _header_rows(grid: list[list[str]]) -> int:
    """表头行数 = 第一个"含数字的单元格占比 ≥60%"的行之前的行数。

    不用横线位置判定：三线表的中线可能被拆成多段只覆盖部分列，
    用"第 1、2 条横线之间"会把表头误切成两半。
    用单元格而非 token 判定：`5.86 %` 会被切成两个 token（数字 + 单位），
    按 token 算占比只有 0.43，会把数据行误当表头。
    """
    for i, row in enumerate(grid):
        cells = [c for c in row if c.strip()]
        if len(cells) < 2:
            continue
        frac = sum(any(ch.isdigit() for ch in c) for c in cells) / len(cells)
        if frac >= 0.60:
            if i == 0:
                return 1          # 首行就是数据 => 没有表头，降级为普通行
            return i if i <= 4 else 1
    return 1


def _build_grid(rows: list[dict], spans: list[list[float]]) -> list[list[str]]:
    """把词按 x 中心落到列上，拼成二维单元格文本。"""
    bounds = [(spans[i][1] + spans[i + 1][0]) / 2.0 for i in range(len(spans) - 1)]
    grid: list[list[str]] = []
    for r in rows:
        cells = [""] * len(spans)
        for w in sorted(r["w"], key=lambda w: w[0]):
            xc = (w[0] + w[2]) / 2.0
            ci = 0
            while ci < len(bounds) and xc > bounds[ci]:
                ci += 1
            cells[ci] = f"{cells[ci]} {w[4]}".strip() if cells[ci] else w[4]
        grid.append(cells)
    return grid


def _extract_table(page: "fitz.Page", region: dict,
                   min_rows: int) -> tuple[str, tuple] | None:
    """把一个表区间还原成 markdown；不可靠时返回 None（宁缺勿滥）。"""
    x0, y0, x1, y1 = region["rect"]
    words = [w for w in page.get_text("words")
             if x0 - 1 <= (w[0] + w[2]) / 2.0 <= x1 + 1
             and y0 + 1 <= (w[1] + w[3]) / 2.0 <= y1 - 1]
    if len(words) < 4:
        return None

    rows = _rows_from_words(words)

    # 正文混入检测：表格行不会很长，一旦出现长行就把其后全部截掉
    # （区间是通过横线圈的，偶尔会跨到正文；不截会污染列切分）
    cut = len(rows)
    for i, r in enumerate(rows):
        if sum(len(w[4]) for w in r["w"]) > PROSE_ROW_CHARS:
            cut = i
            break
    rows = rows[:cut]
    if len(rows) < min_rows:
        return None
    words = [w for r in rows for w in r["w"]]

    # 列边界与表头行数互相依赖：列影响表头判定，表头影响列投影（表头词
    # 会横跨多列、把列间空隙"架桥"）。迭代到稳定，一般 2 次即可。
    spans = _col_spans(words)
    n_head, grid = 1, []
    for _ in range(4):
        if not 2 <= len(spans) <= MAX_COLS:
            return None
        grid = _build_grid(rows, spans)
        new_head = _header_rows(grid)
        if new_head == n_head:
            break
        n_head = new_head
        spans = _col_spans([w for r in rows[n_head:] for w in r["w"]] or words)

    if not 2 <= len(spans) <= MAX_COLS:
        return None
    grid = _build_grid(rows, spans)

    # 多行表头：最后一行才是逐列列名；上面几行多是**跨列分组标题**
    # （如 "Alloy compositions (wt.%)" 压在 3 个元素列上）。按列拼接会把
    # "Alloy" 与 "Mg" 拼成一格 "Alloy Mg"，模型会把 Mg 当成合金牌号取走，
    # 导致整张表的值系统性错位一列。跨列标题单独成行给出，不参与列名。
    labels = grid[n_head - 1] if n_head >= 1 else []
    header: list[str] = []
    for ci in range(len(spans)):
        lab = labels[ci].strip() if ci < len(labels) else ""
        up = " ".join([hr[ci] for hr in grid[:n_head - 1] if hr[ci]]).strip() \
            if n_head > 1 else ""
        # 列名只有单位（如 "(MPa)"）时，把上一行的分组词接回来 -> "UTS (MPa)"
        if lab.startswith("(") and up:
            lab = f"{up} {lab}"
        if not lab:
            lab = up
        header.append(lab)
    header_note = ""
    if n_head > 1:
        toks = [t for r in grid[:n_head - 1] for t in r if t.strip()]
        if toks:
            header_note = " ".join(toks)[:240]
    body = [r for r in grid[n_head:] if any(c.strip() for c in r)]

    # 行一致性：真表的绝大多数行至少有两个非空单元格；图里的散乱标签
    # （被矩形描边误判成表）大多只有一格。单行数据表是合法的，不能按行数卡。
    if not body:
        return None
    if sum(1 for r in body
           if sum(1 for c in r if c.strip()) >= 2) < len(body) * 0.5:
        return None

    # 丢弃整列为空的列（投影切分偶发多切）
    keep = [ci for ci in range(len(spans))
            if header[ci] or any(r[ci] for r in body)]
    if len(keep) < 2:
        return None
    header = [header[ci] for ci in keep]
    body = [[r[ci] for ci in keep] for r in body]
    if len(body) < max(1, min_rows - 1):
        return None
    if not any(h.strip() for h in header):
        return None

    lines: list[str] = []
    cap = _caption(page, region["rect"])
    if cap:
        lines.append(f"[表格] {cap}")
    if header_note:
        lines.append(f"[表头分组] {header_note}（跨列的分组标题，不是列名）")
    lines.append("| " + " | ".join(h.replace("|", "/") for h in header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for r in body:
        lines.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")

    # 返回收缩后的 bbox：只覆盖真正用到的行，避免误删正文块
    used_bottom = max(w[3] for w in words) + 1.5
    return "\n".join(lines), (x0, region["rules"][0] - 1.5, x1, used_bottom)


def _overlaps(bbox: tuple, table_bboxes: list[tuple], ratio: float = 0.5) -> bool:
    x0, y0, x1, y1 = bbox
    area = max(1e-6, (x1 - x0) * (y1 - y0))
    for tx0, ty0, tx1, ty1 in table_bboxes:
        ix = max(0.0, min(x1, tx1) - max(x0, tx0))
        iy = max(0.0, min(y1, ty1) - max(y0, ty0))
        if ix * iy > ratio * area:
            return True
    return False


# ---------------------------------------------------------------- 超长切分

def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？；;.!?])\s*", text)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if len(buf) + len(s) <= max_chars:
            buf += s
        else:
            if buf:
                out.append(buf)
            buf = (buf[-overlap:] if (overlap and buf) else "") + s
    if buf:
        out.append(buf)
    return [x for x in out if x.strip()]


# ---------------------------------------------------------------- 主入口

def parse_pdf(pdf_path: str, cfg: dict) -> tuple[list[Chunk], dict]:
    """解析单个 PDF -> (chunks, meta)。"""
    doc_id = os.path.splitext(os.path.basename(pdf_path))[0]
    doc_name = os.path.basename(pdf_path)

    min_chars = int(cfg["ingest"]["min_chunk_chars"])
    max_chars = int(cfg["ingest"]["max_chunk_chars"])
    overlap = int(cfg["ingest"]["overlap_chars"])
    keep_tables = bool(cfg["ingest"]["keep_tables_separate"])
    table_min_rows = int(cfg["ingest"]["table_min_rows"])
    drop_refs = bool(cfg["ingest"]["drop_references"])
    max_pages = int(cfg["extract"].get("max_pages_per_run", 0) or 0)

    chunks: list[Chunk] = []
    meta = {"doc_id": doc_id, "doc_name": doc_name, "page_count": 0,
            "title": "", "authors": ""}

    with fitz.open(pdf_path) as doc:
        meta["page_count"] = doc.page_count
        try:
            md = doc.metadata or {}
            meta["title"] = md.get("title", "") or ""
            meta["authors"] = md.get("author", "") or ""
        except Exception:
            pass

        limit = doc.page_count if max_pages <= 0 else min(max_pages, doc.page_count)
        for pno in range(limit):
            page = doc[pno]
            page_no = pno + 1

            # ---- 表格 ----
            table_bboxes: list[tuple] = []
            if keep_tables:
                for region in _table_regions(page):
                    made = _extract_table(page, region, table_min_rows)
                    if made is None:
                        continue
                    md_text, bbox = made
                    if not md_text:
                        continue
                    chunks.append(Chunk.make(doc_id, doc_name, page_no,
                                             "table", md_text, bbox))
                    table_bboxes.append(bbox)

            # ---- 正文 ----
            blocks = _reading_order(_collect_blocks(page, drop_refs),
                                    page.rect.width, page.rect.height)
            buf, buf_bbox = "", None
            for b in blocks:
                text, bbox = b["text"], b["bbox"]
                if table_bboxes and _overlaps(bbox, table_bboxes):
                    continue
                if len(text) > max_chars:
                    if len(buf.strip()) >= min_chars:
                        chunks.append(Chunk.make(doc_id, doc_name, page_no,
                                                 "text", buf.strip(), buf_bbox))
                    buf, buf_bbox = "", None
                    for piece in _split_long(text, max_chars, overlap):
                        chunks.append(Chunk.make(doc_id, doc_name, page_no,
                                                 "text", piece, bbox))
                    continue
                if not buf:
                    buf, buf_bbox = text, bbox
                elif len(buf) + len(text) + 1 <= max_chars:
                    buf = f"{buf}\n{text}"
                else:
                    if len(buf.strip()) >= min_chars:
                        chunks.append(Chunk.make(doc_id, doc_name, page_no,
                                                 "text", buf.strip(), buf_bbox))
                    tail = buf[-overlap:] if overlap > 0 else ""
                    buf = f"{tail}\n{text}".strip()
                    buf_bbox = bbox
            if len(buf.strip()) >= min_chars:
                chunks.append(Chunk.make(doc_id, doc_name, page_no,
                                         "text", buf.strip(), buf_bbox))

    return chunks, meta
