"""Antigravity 只读适配：摘要库 + 每会话独立 SQLite，不修改原库、不读取凭据。

数据源（~/.gemini/antigravity/）：
  conversation_summaries.db —— 全部会话的标题/预览/时间清单
  conversations/<uuid>.db   —— 单个会话的 steps 表（protobuf 编码的对话内容）

steps.step_payload 是没有公开 schema 的 protobuf：靠裸 wire-format 解析取字段，
文本字段号（用户 19.2 / 助手 20.3）是实测得来的，上游改版会静默取不到 ——
所以页面顶部横幅依赖 scanner 层的兜底提示，这里只保证"取不到就给空"。
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = os.path.expanduser("~/.gemini/antigravity")
SUMMARY_DB = os.path.join(BASE_DIR, "conversation_summaries.db")
CONV_DIR = os.path.join(BASE_DIR, "conversations")

STEP_USER = 14          # steps.step_type：用户输入
STEP_ASSISTANT = 15     # steps.step_type：助手输出（含工具调用，这里只取文本）


@contextmanager
def _connect(path):
    # 只读 URI 打开：库不存在时不创建、有 WAL 时也能读到已提交内容
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


# ── 裸 protobuf wire-format 解码（不需要 schema）──────────────

def _rv(b, i):
    r, s = 0, 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7f) << s
        if not x & 0x80:
            return r, i
        s += 7
        if s > 63:
            raise ValueError("varint 过长")


def _fields(b):
    out, i = [], 0
    while i < len(b):
        k, i = _rv(b, i)
        fn, wt = k >> 3, k & 7
        if wt == 0:
            v, i = _rv(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            n, i = _rv(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError("wire type %d" % wt)
        out.append((fn, wt, v))
    return out


def _get(b, fn):
    """顶层字段 fn 的全部 wt=2 值；解析失败给空，不让单条坏数据炸整个会话。"""
    try:
        return [v for f, w, v in _fields(b) if f == fn and w == 2]
    except Exception:
        return []


def _text_at(blob, *path):
    """按 字段号路径 下钻取第一个可解码字符串。"""
    cur = [blob]
    for fn in path:
        nxt = []
        for b in cur:
            nxt.extend(_get(b, fn))
        cur = nxt
        if not cur:
            return None
    for v in cur:
        try:
            s = v.decode("utf-8").strip()
        except Exception:
            continue
        if s:
            return s
    return None


def _step_time(blob):
    """steps.step_payload 顶层 field 5.1.1 = epoch 秒；取不到给 None。"""
    for t in _get(blob, 5):
        for m in _get(t, 1):
            try:
                for f, w, v in _fields(m):
                    if f == 1 and w == 0 and 1600000000 < v < 2000000000:
                        return int(v)
            except Exception:
                continue
    return None


# ── 对外接口 ─────────────────────────────────────────────────

def _project_of(row):
    """workspace_uris 非空取第一个路径的目录名；否则项目 ID；再兜底应用名。"""
    ws = (row["workspace_uris"] or "").strip()
    if ws:
        first = re.split(r"[,;]", ws)[0].strip()
        first = re.sub(r"^file:///?", "", first)
        name = os.path.basename(first.rstrip("\\/"))
        if name:
            return name
    pid = (row["project_id"] or "").strip()
    if pid and pid != "outside-of-project":
        return pid
    return "Antigravity"


def _parse_time(value):
    """'2026-09-19 05:06:02.2320262+00:00' → epoch 秒；7 位小数截到 6 位。"""
    if not value:
        return 0.0
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$",
                 str(value).strip())
    if not m:
        return 0.0
    frac = (m.group(2) or "")[:6].ljust(6, "0")
    tz = m.group(3) or "+00:00"
    if tz == "Z":
        tz = "+00:00"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat("%s.%s%s" % (m.group(1), frac, tz))
        return dt.timestamp()
    except Exception:
        return 0.0


def is_managed(sid):
    """这个 uuid 是不是 Antigravity 的会话——给 /api/send 和回收站当判据。"""
    if not isinstance(sid, str) or not re.match(r"^[0-9a-fA-F-]{36}$", sid):
        return False
    if not os.path.isfile(SUMMARY_DB):
        return False
    try:
        with _connect(SUMMARY_DB) as conn:
            return conn.execute(
                "SELECT 1 FROM conversation_summaries WHERE conversation_id=?",
                (sid,)).fetchone() is not None
    except (sqlite3.Error, OSError):
        return False


def scan(path=None):
    """列表只读元数据；运行状态不猜，统一当 idle。"""
    path = path or SUMMARY_DB
    if not os.path.isfile(path):
        return []
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT conversation_id, title, preview, last_modified_time,"
                " workspace_uris, project_id, killed FROM conversation_summaries"
                " ORDER BY last_modified_time DESC"
            ).fetchall()
        out = []
        for row in rows:
            if row["killed"]:
                continue
            title = row["title"] or ""
            out.append({
                "session_id": row["conversation_id"],
                "engine": "antigravity",
                "client": "Antigravity（只读）",
                "entrypoint": "antigravity",
                "custom_title": title,
                "first_msg": "",
                "project": _project_of(row),
                "cwd": "",
                "last_time": _parse_time(row["last_modified_time"]),
                "last_msg": (row["preview"] or "").strip() or "只读会话 · 运行状态未知",
                "status": "idle",
                "can_run": False,
            })
        return out
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return []


def history(sid, max_turns=30):
    """提取 user/assistant 可见文本；工具调用、合成内容一律跳过。"""
    if not isinstance(sid, str) or not re.match(r"^[0-9a-fA-F-]{36}$", sid):
        return None
    db_path = os.path.join(CONV_DIR, "%s.db" % sid)
    if not os.path.isfile(db_path):
        return None
    try:
        want = max(1, min(int(max_turns), 200))
        turns = []
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT step_type, step_payload FROM steps"
                " WHERE step_type IN (?, ?) ORDER BY idx DESC",
                (STEP_USER, STEP_ASSISTANT),
            ).fetchall()
        for row in reversed(rows):
            blob = row["step_payload"] or b""
            if row["step_type"] == STEP_USER:
                text = _text_at(blob, 19, 2)
                role = "user"
            else:
                text = _text_at(blob, 20, 3)
                role = "assistant"
            if not text:
                continue
            turns.append({"role": role, "text": text[:4000],
                          "time": _step_time(blob) or 0.0})
            if len(turns) >= want * 2:
                break
        if not turns:
            return None
        meta = None
        try:
            with _connect(SUMMARY_DB) as conn:
                meta = conn.execute(
                    "SELECT title, workspace_uris, project_id FROM conversation_summaries"
                    " WHERE conversation_id=?", (sid,)).fetchone()
        except (sqlite3.Error, OSError):
            meta = None
        title = meta["title"] if meta else ""
        return {"session_id": sid, "engine": "antigravity",
                "title": title, "cwd": "",
                "project": _project_of(meta) if meta else "Antigravity",
                "turns": turns, "can_run": False}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
