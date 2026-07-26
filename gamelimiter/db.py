"""SQLite 层：游戏 + 规则配置、游玩会话、事件记录。

守护进程与 GUI/CLI 跨进程共享同一 DB，WAL 模式 + busy_timeout 解决并发。
时间戳统一 unix 秒（int）。
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    exe_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    exe_path TEXT,
    cooldown_hours REAL,      -- 规则a 间隔冷却，NULL=未启用
    session_minutes REAL,     -- 规则b 单次最长时长（上限），NULL=未启用
    next_session_minutes REAL,-- 下次会话的一次性额度（≤上限），守护开会话时消费；NULL=用满上限
    windows TEXT,             -- 规则c 允许时段，JSON 数组 ["19:00-23:00"]，NULL=不限
    icon TEXT,                -- 从 exe 提取的 PNG data URI，NULL=没取到（GUI 退回首字母块）
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id),
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    end_reason TEXT,          -- self_exit / session_timeout / window_end / disabled / daemon_restart
    limit_minutes REAL        -- 本次生效额度快照，NULL=用满上限；进行中只可改小
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,       -- blocked / killed / warn / daemon_start ...
    detail TEXT
);
CREATE TABLE IF NOT EXISTS pending_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    field TEXT NOT NULL,      -- cooldown_hours / session_minutes / windows / enabled / __delete__
    value TEXT,               -- JSON，如 {"v": 2}；__delete__ 为 NULL
    apply_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(game_id, field)
);
"""


@dataclass
class Game:
    id: int
    name: str
    exe_name: str
    exe_path: Optional[str]
    cooldown_hours: Optional[float]
    session_minutes: Optional[float]   # 单次最长（上限）
    windows: Optional[list]   # ["19:00-23:00", ...]
    enabled: bool
    icon: Optional[str] = None
    next_session_minutes: Optional[float] = None


# 老库补列：(表, 列, 类型)。ALTER 幂等靠 duplicate column 异常兜底——守护与 GUI
# 可能同时开库，先查 table_info 再 ALTER 仍有竞态窗口
_MIGRATIONS = [("games", "icon", "TEXT"),
               ("games", "next_session_minutes", "REAL"),
               ("sessions", "limit_minutes", "REAL")]


def _migrate(conn):
    for table, col, type_ in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _row_to_game(r: sqlite3.Row) -> Game:
    return Game(
        id=r["id"], name=r["name"], exe_name=r["exe_name"], exe_path=r["exe_path"],
        cooldown_hours=r["cooldown_hours"], session_minutes=r["session_minutes"],
        windows=json.loads(r["windows"]) if r["windows"] else None,
        enabled=bool(r["enabled"]), icon=r["icon"],
        next_session_minutes=r["next_session_minutes"],
    )


def list_games(conn, enabled_only: bool = False) -> list[Game]:
    sql = "SELECT * FROM games" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return [_row_to_game(r) for r in conn.execute(sql)]


def get_game(conn, exe_name: str) -> Optional[Game]:
    r = conn.execute("SELECT * FROM games WHERE exe_name=? COLLATE NOCASE", (exe_name,)).fetchone()
    return _row_to_game(r) if r else None


def upsert_game(conn, name, exe_name, exe_path=None,
                cooldown_hours=None, session_minutes=None, windows=None, icon=None) -> Game:
    now = int(time.time())
    conn.execute(
        """INSERT INTO games (name, exe_name, exe_path, cooldown_hours, session_minutes, windows,
                              icon, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(exe_name) DO UPDATE SET
             name=excluded.name, exe_path=excluded.exe_path,
             cooldown_hours=excluded.cooldown_hours,
             session_minutes=excluded.session_minutes,
             windows=excluded.windows,
             icon=COALESCE(excluded.icon, games.icon),   -- 没带图标时不清掉已有的
             updated_at=excluded.updated_at""",
        (name, exe_name, exe_path, cooldown_hours, session_minutes,
         json.dumps(windows) if windows else None, icon, now, now))
    conn.commit()
    return get_game(conn, exe_name)


def set_icon(conn, game_id: int, icon: Optional[str]):
    """只改图标，不动 updated_at（补图标不算规则变更）。"""
    conn.execute("UPDATE games SET icon=? WHERE id=?", (icon, game_id))
    conn.commit()


def set_next_session(conn, game_id: int, minutes: Optional[float]):
    """设置/清除下次会话的一次性额度。不动 updated_at——这不是规则变更，
    额度永远只能比上限更严，走不到放宽延迟那套（校验在 changes 层）。"""
    conn.execute("UPDATE games SET next_session_minutes=? WHERE id=?", (minutes, game_id))
    conn.commit()


def update_rules(conn, game_id: int, **fields):
    """fields 可含 cooldown_hours / session_minutes / windows / enabled / name。"""
    allowed = {"cooldown_hours", "session_minutes", "windows", "enabled", "name"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"unknown field {k}")
        if k == "windows" and v is not None:
            v = json.dumps(v)
        sets.append(f"{k}=?")
        vals.append(v)
    sets.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(game_id)
    conn.execute(f"UPDATE games SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()


def remove_game(conn, game_id: int):
    conn.execute("DELETE FROM games WHERE id=?", (game_id,))
    conn.execute("DELETE FROM pending_changes WHERE game_id=?", (game_id,))
    conn.commit()


# ---- 会话 ----

def open_session(conn, game_id: int, start_ts: int,
                 limit_minutes: Optional[float] = None) -> int:
    cur = conn.execute("INSERT INTO sessions (game_id, start_ts, limit_minutes) VALUES (?,?,?)",
                       (game_id, start_ts, limit_minutes))
    conn.commit()
    return cur.lastrowid


def set_session_limit(conn, session_id: int, minutes: Optional[float]):
    """改进行中会话的额度（只允许缩短，校验在 changes 层）。"""
    conn.execute("UPDATE sessions SET limit_minutes=? WHERE id=?", (minutes, session_id))
    conn.commit()


def close_session(conn, session_id: int, end_ts: int, reason: str):
    conn.execute("UPDATE sessions SET end_ts=?, end_reason=? WHERE id=?", (end_ts, reason, session_id))
    conn.commit()


def open_sessions(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sessions WHERE end_ts IS NULL").fetchall()


def active_session(conn, game_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE game_id=? AND end_ts IS NULL", (game_id,)).fetchone()


def last_session_end(conn, game_id: int) -> Optional[int]:
    r = conn.execute("SELECT MAX(end_ts) AS m FROM sessions WHERE game_id=?", (game_id,)).fetchone()
    return r["m"]


# ---- 待生效变更（规则放宽延迟）----

def upsert_pending(conn, game_id: int, field: str, value, apply_at: int):
    now = int(time.time())
    conn.execute(
        """INSERT INTO pending_changes (game_id, field, value, apply_at, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(game_id, field) DO UPDATE SET
             value=excluded.value, apply_at=excluded.apply_at, created_at=excluded.created_at""",
        (game_id, field, json.dumps({"v": value}), apply_at, now))
    conn.commit()


def list_pending(conn, game_id: Optional[int] = None) -> list[sqlite3.Row]:
    if game_id is None:
        return conn.execute("SELECT * FROM pending_changes ORDER BY apply_at").fetchall()
    return conn.execute("SELECT * FROM pending_changes WHERE game_id=? ORDER BY apply_at",
                        (game_id,)).fetchall()


def due_pendings(conn, now: float) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pending_changes WHERE apply_at<=?", (int(now),)).fetchall()


def delete_pending(conn, pending_id: int):
    conn.execute("DELETE FROM pending_changes WHERE id=?", (pending_id,))
    conn.commit()


def clear_pending_field(conn, game_id: int, field: str):
    conn.execute("DELETE FROM pending_changes WHERE game_id=? AND field=?", (game_id, field))
    conn.commit()


def clear_pending_game(conn, game_id: int):
    conn.execute("DELETE FROM pending_changes WHERE game_id=?", (game_id,))
    conn.commit()


def log_event(conn, game_id: Optional[int], type_: str, detail: str = ""):
    conn.execute("INSERT INTO events (game_id, ts, type, detail) VALUES (?,?,?,?)",
                 (game_id, int(time.time()), type_, detail))
    conn.commit()
