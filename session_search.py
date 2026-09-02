# -*- coding: utf-8 -*-
"""会话内容全文搜索：SQLite FTS 索引，按文件 mtime 增量更新。

为什么单独一个模块：session_scanner 管「列表要的元信息」（轻、快），
这里管「内容检索」（重：要读全文件）。两件事的 IO 特征完全不同，
混在一起会把列表轮询拖慢。

索引策略：
- 每个文件记 mtime；没变整份跳过，变了就删旧插新（JSONL 只追加不修改，
  所以 mtime 是可靠的变更信号）
- update_index() 每轮最多处理 _FILES_PER_ROUND 个文件（按新→旧排队），
  由 web_server 后台线程周期调用 —— 单轮秒级，不抢主业务 IO

分词用 trigram：默认 unicode61 对中文基本不可用（一长串切不开，
搜子串必空）；trigram 让任意 ≥3 字子串都能命中。更短的查询降级 LIKE。
"""

import glob
import io
import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
DB_PATH = os.path.join(HERE, "session_index.db")

_FILES_PER_ROUND = 40     # 每轮最多重建几个文件的索引

import session_scanner as S


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, mtime REAL, sid TEXT, engine TEXT)""")
    try:
        # trigram 分词是子串搜索的前提。老 SQLite 没有 FTS5/trigram 时
        # 建表失败不致命 —— search() 会探测并降级 LIKE
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS msgs
            USING fts5(sid UNINDEXED, role UNINDEXED, text, path UNINDEXED,
                       tokenize='trigram')""")
    except Exception:
        pass
    return conn


def _fts_ok(conn):
    try:
        conn.execute("SELECT 1 FROM msgs LIMIT 1")
        return True
    except Exception:
        return False


def extract_messages(fpath):
    """从一份会话文件里全量抽 (role, text)。三种格式同一套宽容原则。

    与扫描器共用注入过滤和文本提取函数 —— 格式再变只改一处。
    """
    out = []
    with io.open(fpath, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"text' not in line and '"message"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            role, t = None, ""
            p = r.get("payload")
            if isinstance(p, dict):
                # Codex：新旧两种格式
                if p.get("type") == "message":
                    role = p.get("role")
                    t = S._text_from_codex_content(p.get("content"))
                elif p.get("type") == "user_message":
                    role, t = "user", str(p.get("message") or "")
                elif p.get("type") == "agent_message":
                    role, t = "assistant", str(p.get("message") or "")
            else:
                # Claude / Pi：message 包在顶层 message 字段里
                m = r.get("message")
                if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                    role = m["role"]
                    t = S._text_from_content(m.get("content"))
            t = (t or "").strip()
            if role and len(t) > 1 and not S._codex_injected(t):
                out.append((role, t[:4000]))
    return out


def _engine_of_path(path):
    low = path.lower()
    if os.sep + ".codex" + os.sep in low or ".codex" in low:
        return "codex"
    if ".pi" + os.sep in low:
        return "pi"
    return "claude"


def _all_paths():
    paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))
    paths += glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*",
                                    "rollout-*.jsonl"))
    for src in S.EXTRA_SOURCES:
        paths += glob.glob(os.path.join(src["dir"], "**", "*.jsonl"),
                           recursive=True)
    return paths


def update_index(max_files=_FILES_PER_ROUND):
    """增量更新索引。返回 {"pending": 还剩几个没处理}。

    按 mtime 新→旧排队，每轮只啃 max_files 个 —— 刚在跑的任务最先被收进索引，
    历史包袱慢慢消化。
    """
    conn = _conn()
    known = {r[0]: r[1] for r in
             conn.execute("SELECT path, mtime FROM files")}
    todo = []
    live = set()
    for fpath in _all_paths():
        live.add(fpath)
        try:
            m = os.path.getmtime(fpath)
        except Exception:
            continue
        if abs(known.get(fpath, -1e9) - m) < 1:      # 没变，跳过
            continue
        todo.append((m, fpath))
    todo.sort(reverse=True)

    for m, fpath in todo[:max_files]:
        base = os.path.basename(fpath)
        sid = S._sid_from_name(base) or (
            base[:-6] if base.endswith(".jsonl") else base)
        msgs = extract_messages(fpath)
        conn.execute("DELETE FROM msgs WHERE path=?", (fpath,))
        conn.executemany(
            "INSERT INTO msgs(sid, role, text, path) VALUES (?,?,?,?)",
            [(sid, role, t, fpath) for role, t in msgs])
        conn.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                     (fpath, m, sid, _engine_of_path(fpath)))

    # 文件已消失的清残留（卸载/归档过的客户端目录）
    gone = [p for p in known if p not in live][:200]
    for p in gone:
        conn.execute("DELETE FROM msgs WHERE path=?", (p,))
        conn.execute("DELETE FROM files WHERE path=?", (p,))
    conn.commit()
    return {"pending": max(0, len(todo) - max_files), "processed": min(len(todo), max_files)}


def search(q, limit=20):
    """搜内容。返回 [{sid,title,snippet,when}]，按会话去重。

    ≥3 字走 FTS trigram（真子串匹配），更短的降级 LIKE 全表扫——
    索引几十万行时 LIKE 也就几百毫秒，短查询低频可接受。
    """
    q = (q or "").strip()
    if not q:
        return []
    conn = _conn()
    rows = []
    if len(q) >= 3 and _fts_ok(conn):
        try:
            rows = conn.execute(
                "SELECT sid, text, path FROM msgs WHERE msgs MATCH ? "
                "ORDER BY rowid DESC LIMIT ?", ('"' + q.replace('"', '""') + '"',
                                                limit)).fetchall()
        except Exception:
            rows = []
    if not rows:
        rows = conn.execute(
            "SELECT sid, text, path FROM msgs WHERE text LIKE ? LIMIT ?",
            ("%" + q + "%", limit)).fetchall()

    titles = {s["session_id"]: s for s in S.scan_all_cached("all")}
    seen, out = set(), []
    for sid, text, _path in rows:
        if sid in seen:
            continue
        seen.add(sid)
        meta = titles.get(sid) or {}
        out.append({
            "sid": sid,
            "title": meta.get("title") or sid[:8],
            "snippet": text[:160],
            "when": S.time_ago(meta.get("last_time") or 0),
        })
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        info = update_index()
        print("索引一轮：处理 %d 个，还剩 %d 个待处理"
              % (info["processed"], info["pending"]))
        for r in search(sys.argv[1]):
            print("· %s（%s）\n  %s" % (r["title"], r["when"], r["snippet"][:100]))
    else:
        print(__doc__)
