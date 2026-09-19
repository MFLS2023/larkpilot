"""Zcode CLI SQLite 会话只读适配，不修改原库、不读取凭据。"""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.path.expanduser("~/.zcode/cli/db/db.sqlite")


@contextmanager
def _connect(path):
    # 保留 WAL 可见性，不用 immutable；原库不存在时不创建。
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _object(value):
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def scan(path=None):
    """列表只读元数据；客户端运行状态未知，不猜测已完成或正在跑。"""
    path = path or DB_PATH
    if not os.path.isfile(path):
        return []
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT id, directory, title, time_updated FROM session "
                "WHERE time_archived IS NULL ORDER BY time_updated DESC"
            ).fetchall()
        return [{
            "session_id": row["id"], "engine": "zcode", "client": "Zcode（只读）",
            "entrypoint": "zcode", "custom_title": row["title"] or "",
            "first_msg": "", "project": os.path.basename(row["directory"].rstrip("\\/")) or "Zcode",
            "cwd": row["directory"], "last_time": float(row["time_updated"] or 0) / 1000,
            "last_msg": "只读会话 · 运行状态未知", "status": "idle", "can_run": False,
        } for row in rows]
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return []


def history(sid, max_turns=30, path=None):
    """读取可见文本，跳过合成消息、推理和工具输出。"""
    path = path or DB_PATH
    if not isinstance(sid, str) or not sid.startswith("sess_") or not os.path.isfile(path):
        return None
    try:
        limit = max(1, min(int(max_turns), 200)) * 2
        with _connect(path) as conn:
            row = conn.execute("SELECT directory, title FROM session WHERE id=?", (sid,)).fetchone()
            if row is None:
                return None
            messages = conn.execute(
                "SELECT id,data,time_created FROM message WHERE session_id=? "
                "ORDER BY COALESCE(sequence,time_created) DESC,time_created DESC,id DESC LIMIT ?",
                (sid, limit),
            ).fetchall()
            turns = []
            for message in reversed(messages):
                meta = _object(message["data"])
                role = meta.get("role")
                if role not in ("user", "assistant") or meta.get("synthetic"):
                    continue
                parts = conn.execute(
                    "SELECT data FROM part WHERE session_id=? AND message_id=? "
                    "ORDER BY COALESCE(sequence,time_created),time_created,id",
                    (sid, message["id"]),
                ).fetchall()
                text = []
                for part in parts:
                    data = _object(part["data"])
                    if data.get("type") == "text" and not data.get("synthetic") and isinstance(data.get("text"), str):
                        text.append(data["text"])
                joined = "".join(text).strip()
                if joined:
                    turns.append({"role": role, "text": joined[:4000], "time": float(message["time_created"] or 0) / 1000})
        return {"session_id": sid, "engine": "zcode", "title": row["title"],
                "cwd": row["directory"],
                "project": os.path.basename(row["directory"].rstrip("\\/")) or "Zcode",
                "turns": turns, "can_run": False}
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
