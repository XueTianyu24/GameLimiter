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
    exe_size INTEGER,         -- 登记时 exe 的字节数；改名/搬移后靠它做第一道识别（见 procmatch）
    exe_hash TEXT,            -- exe 首尾各 1MB + 大小的 sha256；size 撞车时的确认位
    cooldown_hours REAL,      -- 规则a 间隔冷却（小时级，同日兜底），NULL=未启用
    next_allowed_date TEXT,   -- 规则a 第二道门：下次可玩日 'YYYY-MM-DD'，过期即失效，NULL=不限
    session_minutes REAL,     -- 规则b 单次最长时长（上限），NULL=未启用
    next_session_minutes REAL,-- 下次会话的一次性额度（≤上限），守护开会话时消费；NULL=用满上限
    windows TEXT,             -- 规则c 允许时段，JSON 数组 ["19:00-23:00"]，NULL=不限
    icon TEXT,                -- 从 exe 提取的 PNG data URI，NULL=没取到（GUI 退回首字母块）
    monitor_only INTEGER NOT NULL DEFAULT 0,  -- 观察模式：只采集帧/硬件数据，不施加任何限制，
                              -- 也不占「每天最多玩几款」的名额。给 PVP 这类强杀会判逃跑的游戏用
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
    limit_minutes REAL,       -- 本次生效额度快照，NULL=用满上限；进行中只可改小
    block_id INTEGER,         -- 所属游玩段（= 该段首个 session 的 id）；同段内续玩共享额度
    played_seconds REAL,      -- 守护心跳累计的**真实**在跑秒数（空窗期不计），NULL=老数据
    last_seen_ts INTEGER      -- 最后一次观测到进程存活的时刻；会话结束时间取它
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,       -- blocked / killed / warn / daemon_start ...
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,     -- 全局设置（跨游戏）：daily_game_limit / daily_minutes[_weekend] / 采集开关
    value TEXT                -- NULL = 未设/不限
);
CREATE TABLE IF NOT EXISTS frame_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,   -- 一次进程运行对应一条（一段游玩可含多条）
    game_id INTEGER NOT NULL,
    block_id INTEGER,              -- 冗余存一份，按段聚合时免去 join sessions
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    frames INTEGER,                -- 采到的帧数；0 = 挂上了但没采到（如游戏没渲染就退了）
    seconds REAL,                  -- 采样覆盖的秒数
    status TEXT,                   -- ok / no_frames / truncated / failed
    summary TEXT                   -- JSON 摘要：分位数 / 瓶颈 / 画面模式 / 每分钟趋势
);
CREATE INDEX IF NOT EXISTS idx_frame_runs_game ON frame_runs(game_id, id DESC);
CREATE TABLE IF NOT EXISTS hw_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    block_id INTEGER,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    samples INTEGER,               -- 1Hz 采样点数
    csv_path TEXT,                 -- 原始逐秒 CSV（**保留**，两小时才约 700KB，供事后分析）
    summary TEXT                   -- JSON：CPU/内存/磁盘/GPU 分位数 + 干扰进程 + 异常标记
);
CREATE INDEX IF NOT EXISTS idx_hw_runs_game ON hw_runs(game_id, id DESC);
CREATE TABLE IF NOT EXISTS pending_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    field TEXT NOT NULL,      -- cooldown_hours / session_minutes / windows / enabled / __delete__
    value TEXT,               -- JSON，如 {"v": 2}；__delete__ 为 NULL
    apply_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(game_id, field)
);
CREATE TABLE IF NOT EXISTS capture_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL,
    duration_minutes REAL,         -- 采多久；NULL = 一直采到游戏退出
    out_dir TEXT,                  -- 数据落脚目录；NULL = 默认数据目录
    keep_raw INTEGER NOT NULL DEFAULT 1,  -- 保留原始帧 CSV（手动采集默认留，一小时约 260MB）
    state TEXT NOT NULL,           -- armed（待命）/ running / done / cancelled / expired
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,   -- 待命超时：这么久还没等到游戏开就作废
    session_id INTEGER,            -- 实际挂上的会话
    started_ts INTEGER,
    ended_ts INTEGER,
    note TEXT                      -- 结束原因（到点 / 游戏退出 / 手动停止 / 失败原因）
);
CREATE INDEX IF NOT EXISTS idx_capture_jobs_game ON capture_jobs(game_id, id DESC);
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
    next_allowed_date: Optional[str] = None    # 'YYYY-MM-DD'
    monitor_only: bool = False                 # 只观察不限制
    exe_size: Optional[int] = None             # exe 指纹：字节数
    exe_hash: Optional[str] = None             # exe 指纹：首尾 1MB + 大小的 sha256


# 老库补列：(表, 列, 类型)。ALTER 幂等靠 duplicate column 异常兜底——守护与 GUI
# 可能同时开库，先查 table_info 再 ALTER 仍有竞态窗口
_MIGRATIONS = [("games", "icon", "TEXT"),
               ("games", "next_session_minutes", "REAL"),
               ("games", "next_allowed_date", "TEXT"),
               ("sessions", "limit_minutes", "REAL"),
               ("sessions", "block_id", "INTEGER"),
               ("sessions", "played_seconds", "REAL"),
               ("sessions", "last_seen_ts", "INTEGER"),
               ("games", "monitor_only", "INTEGER NOT NULL DEFAULT 0"),
               ("games", "exe_size", "INTEGER"),
               ("games", "exe_hash", "TEXT")]


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
        next_allowed_date=r["next_allowed_date"],
        monitor_only=bool(r["monitor_only"]),
        exe_size=r["exe_size"], exe_hash=r["exe_hash"],
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


def set_exe_fingerprint(conn, game_id: int, size: Optional[int], digest: Optional[str]):
    """记下 exe 的大小与指纹，不动 updated_at（补指纹不算规则变更）。

    用途见 `procmatch`：exe 被改名或复制到别处时，靠这两个值仍能认出是同一款游戏。
    """
    conn.execute("UPDATE games SET exe_size=?, exe_hash=? WHERE id=?", (size, digest, game_id))
    conn.commit()


def set_next_session(conn, game_id: int, minutes: Optional[float]):
    """设置/清除下次会话的一次性额度。不动 updated_at——这不是规则变更，
    额度永远只能比上限更严，走不到放宽延迟那套（校验在 changes 层）。"""
    conn.execute("UPDATE games SET next_session_minutes=? WHERE id=?", (minutes, game_id))
    conn.commit()


def update_rules(conn, game_id: int, **fields):
    """fields 可含 cooldown_hours / next_allowed_date / session_minutes / windows / enabled / name。"""
    allowed = {"cooldown_hours", "next_allowed_date", "session_minutes",
               "windows", "enabled", "name", "monitor_only"}
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
                 limit_minutes: Optional[float] = None,
                 block_id: Optional[int] = None) -> int:
    """开一次会话。`block_id=None` = 开新的一段游玩（自己当段首）；
    给了值 = 接着那一段玩（额度继续消耗，不重新发）。"""
    cur = conn.execute(
        """INSERT INTO sessions (game_id, start_ts, limit_minutes, block_id,
                                 played_seconds, last_seen_ts)
           VALUES (?,?,?,?,0,?)""",
        (game_id, start_ts, limit_minutes, block_id, start_ts))
    sid = cur.lastrowid
    if block_id is None:
        conn.execute("UPDATE sessions SET block_id=? WHERE id=?", (sid, sid))
    conn.commit()
    return sid


def heartbeat(conn, session_id: int, played_seconds: float, last_seen_ts: float):
    """守护每轮落库：本次会话累计的真实在跑秒数 + 最后一次看到进程的时刻。"""
    conn.execute("UPDATE sessions SET played_seconds=?, last_seen_ts=? WHERE id=?",
                 (played_seconds, int(last_seen_ts), session_id))
    conn.commit()


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


# ---- 游玩段（block）：连续几次开关游戏算同一段，共享一份额度 ----

def session_played(r: sqlite3.Row) -> float:
    """一条会话行的真实游玩秒数。老数据没有心跳 → 退回墙钟差（那时也没别的可信来源）。"""
    if r["played_seconds"] is not None:
        return max(0.0, r["played_seconds"])
    return max(0.0, (r["end_ts"] - r["start_ts"])) if r["end_ts"] else 0.0


def block_of(r: sqlite3.Row) -> int:
    """会话所属段 id；老数据没有 block_id，各自成段。"""
    return r["block_id"] if r["block_id"] is not None else r["id"]


def last_session(conn, game_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE game_id=? ORDER BY start_ts DESC, id DESC LIMIT 1",
        (game_id,)).fetchone()


def block_rows(conn, block_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE COALESCE(block_id, id)=? ORDER BY id", (block_id,)).fetchall()


def current_block(conn, game_id: int) -> Optional[dict]:
    """该游戏最近一段游玩的聚合。从没玩过 → None。

    `limit_minutes` 取段内各会话额度的最小值——额度只可能被改小，取 min 即当前生效值。
    """
    last = last_session(conn, game_id)
    if not last:
        return None
    rows = block_rows(conn, block_of(last))
    limits = [r["limit_minutes"] for r in rows if r["limit_minutes"] is not None]
    return {
        "block_id": block_of(last),
        "played_seconds": sum(session_played(r) for r in rows),
        "limit_minutes": min(limits) if limits else None,
        "last_end_ts": last["end_ts"],          # None = 这一刻还在玩
        "running": last["end_ts"] is None,
        "sessions": len(rows),
    }


# ---- 帧时间采集 ----

def insert_frame_run(conn, session_id: int, game_id: int, block_id: Optional[int],
                     start_ts: int, end_ts: int, summary: dict, status: str) -> int:
    cur = conn.execute(
        """INSERT INTO frame_runs (session_id, game_id, block_id, start_ts, end_ts,
                                   frames, seconds, status, summary)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (session_id, game_id, block_id, start_ts, end_ts,
         summary.get("frames") or 0, summary.get("seconds") or 0.0,
         status, json.dumps(summary, ensure_ascii=False) if summary else None))
    conn.commit()
    return cur.lastrowid


def frame_runs(conn, game_id: Optional[int] = None, limit: int = 20) -> list[sqlite3.Row]:
    if game_id is None:
        return conn.execute("SELECT * FROM frame_runs ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return conn.execute("SELECT * FROM frame_runs WHERE game_id=? ORDER BY id DESC LIMIT ?",
                        (game_id, limit)).fetchall()


def frame_runs_of_block(conn, block_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM frame_runs WHERE block_id=? AND frames>0 ORDER BY id", (block_id,)).fetchall()


def block_frame_summary(conn, block_id: int) -> Optional[dict]:
    """一段游玩的帧摘要：段内多次进程运行按帧数加权合并。

    分位数无法真正合并（只有摘要没有原始帧），所以按帧数加权取近似——
    段内各次运行的画质设置通常一致，加权平均够用；只有一条时就是精确值。
    """
    rows = frame_runs_of_block(conn, block_id)
    if not rows:
        return None
    from . import frames as _frames
    parts = [(r, _frames.load_summary(r)) for r in rows]
    parts = [(r, s) for r, s in parts if s.get("frames")]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0][1]
    total = sum(s["frames"] for _, s in parts)
    out = dict(parts[0][1])

    def wavg(key):
        vals = [(s.get(key), s["frames"]) for _, s in parts if s.get(key) is not None]
        return round(sum(v * w for v, w in vals) / sum(w for _, w in vals), 2) if vals else None

    for k in ("fps_avg", "fps_low1", "fps_low01", "ft_p50", "ft_p95", "ft_p99",
              "cpu_busy_p50", "gpu_busy_p50", "gpu_wait_p50", "judder_pct",
              "generated_pct", "click_to_photon_p50"):
        v = wavg(k)
        if v is not None:
            out[k] = v
    out["frames"] = total
    out["seconds"] = round(sum(s.get("seconds") or 0 for _, s in parts), 1)
    out["hitches"] = sum(s.get("hitches") or 0 for _, s in parts)
    out["ft_max"] = max(s.get("ft_max") or 0 for _, s in parts)
    secs = out["seconds"]
    out["hitches_per_min"] = round(out["hitches"] / (secs / 60.0), 2) if secs >= 1 else 0.0
    out["runs"] = len(parts)
    out.pop("per_minute", None)          # 跨次运行的时间轴接不起来，段级不给趋势
    return out


# ---- 硬件采集 ----

def insert_hw_run(conn, session_id: int, game_id: int, block_id: Optional[int],
                  start_ts: int, end_ts: int, samples: int, csv_path: str,
                  summary: dict) -> int:
    cur = conn.execute(
        """INSERT INTO hw_runs (session_id, game_id, block_id, start_ts, end_ts,
                                samples, csv_path, summary)
           VALUES (?,?,?,?,?,?,?,?)""",
        (session_id, game_id, block_id, start_ts, end_ts, samples, csv_path,
         json.dumps(summary, ensure_ascii=False) if summary else None))
    conn.commit()
    return cur.lastrowid


def hw_runs(conn, game_id: Optional[int] = None, limit: int = 20) -> list[sqlite3.Row]:
    if game_id is None:
        return conn.execute("SELECT * FROM hw_runs ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return conn.execute("SELECT * FROM hw_runs WHERE game_id=? ORDER BY id DESC LIMIT ?",
                        (game_id, limit)).fetchall()


# ---- 采集任务（手动采集：GUI/CLI 下单，守护接单）----
#
# 守护是 SYSTEM 身份的独立进程，GUI 是用户身份的另一个进程，两边只能靠这张表通信：
# GUI 写一条 armed 记录，守护下一轮（≤1 秒）读到就挂采集器。
# 状态流转：armed →（游戏在跑）running →（到点 / 游戏退出 / 手动停）done
#           armed →（超时没等到游戏）expired；armed/running →（用户取消）cancelled

CAPTURE_STATES_ACTIVE = ("armed", "running")


def create_capture_job(conn, game_id: int, duration_minutes: Optional[float],
                       out_dir: Optional[str], keep_raw: bool, expires_at: int) -> int:
    """下一单采集。同一游戏同时只留一条活跃任务——新的顶掉旧的待命任务。"""
    now = int(time.time())
    conn.execute(
        """UPDATE capture_jobs SET state='cancelled', ended_ts=?, note='被新任务顶替'
           WHERE game_id=? AND state='armed'""", (now, game_id))
    cur = conn.execute(
        """INSERT INTO capture_jobs (game_id, duration_minutes, out_dir, keep_raw,
                                     state, created_at, expires_at)
           VALUES (?,?,?,?,'armed',?,?)""",
        (game_id, duration_minutes, out_dir or None, int(bool(keep_raw)), now, int(expires_at)))
    conn.commit()
    return cur.lastrowid


def armed_capture_job(conn, game_id: int, now: float) -> Optional[sqlite3.Row]:
    """该游戏待命中且没过期的采集任务。守护每轮查它决定要不要挂采集器。"""
    return conn.execute(
        """SELECT * FROM capture_jobs WHERE game_id=? AND state='armed' AND expires_at>?
           ORDER BY id DESC LIMIT 1""", (game_id, int(now))).fetchone()


def active_capture_job(conn, game_id: int) -> Optional[sqlite3.Row]:
    """待命中或正在采的任务（给 GUI 显示状态用）。"""
    return conn.execute(
        """SELECT * FROM capture_jobs WHERE game_id=? AND state IN ('armed','running')
           ORDER BY id DESC LIMIT 1""", (game_id,)).fetchone()


def capture_job(conn, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM capture_jobs WHERE id=?", (job_id,)).fetchone()


def start_capture_job(conn, job_id: int, session_id: int, started_ts: int):
    conn.execute(
        """UPDATE capture_jobs SET state='running', session_id=?, started_ts=?
           WHERE id=? AND state='armed'""", (session_id, started_ts, job_id))
    conn.commit()


def finish_capture_job(conn, job_id: int, state: str = "done", note: str = ""):
    conn.execute(
        """UPDATE capture_jobs SET state=?, ended_ts=?, note=?
           WHERE id=? AND state IN ('armed','running')""",
        (state, int(time.time()), note or None, job_id))
    conn.commit()


def cancel_capture_job(conn, job_id: int, note: str = "手动停止") -> bool:
    """用户按下"停止采集"。running 的任务由守护下一轮看到状态变化后收尾。"""
    cur = conn.execute(
        """UPDATE capture_jobs SET state='cancelled', ended_ts=?, note=?
           WHERE id=? AND state IN ('armed','running')""",
        (int(time.time()), note, job_id))
    conn.commit()
    return cur.rowcount > 0


def expire_capture_jobs(conn, now: float) -> int:
    """待命超时的任务作废——点了采集却一直没开游戏，不该几天后突然采一场。"""
    cur = conn.execute(
        """UPDATE capture_jobs SET state='expired', ended_ts=?, note='一直没等到游戏启动'
           WHERE state='armed' AND expires_at<=?""", (int(now), int(now)))
    conn.commit()
    return cur.rowcount


def abandon_running_capture_jobs(conn) -> int:
    """守护启动时清理上次留下的 running 任务。

    守护崩了/被杀时采集器跟着没了，那条任务永远等不到收尾。不改成"接着采"是因为
    数据已经断成两截、时长也对不上——如实标记中断，让用户自己决定要不要重采。
    """
    cur = conn.execute(
        """UPDATE capture_jobs SET state='done', ended_ts=?, note='守护重启，采集中断'
           WHERE state='running'""", (int(time.time()),))
    conn.commit()
    return cur.rowcount


def capture_jobs(conn, game_id: Optional[int] = None, limit: int = 20) -> list[sqlite3.Row]:
    if game_id is None:
        return conn.execute("SELECT * FROM capture_jobs ORDER BY id DESC LIMIT ?",
                            (limit,)).fetchall()
    return conn.execute("SELECT * FROM capture_jobs WHERE game_id=? ORDER BY id DESC LIMIT ?",
                        (game_id, limit)).fetchall()


# ---- 全局设置 ----

DAILY_GAME_LIMIT = "daily_game_limit"
DAILY_MINUTES = "daily_minutes"        # 规则 e：一天内所有游戏加起来最多玩多少分钟（平日）
DAILY_MINUTES_WEEKEND = "daily_minutes_weekend"   # 规则 e 的周末档；未设 = 周末沿用平日值
CAPTURE_MODE = "capture_mode"          # manual（默认）= 点了才采；auto = 开游戏就采
CAPTURE_OUT_DIR = "capture_out_dir"    # 上次用的存放目录，作为下次下单的默认值


def get_setting(conn, key: str) -> Optional[str]:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(conn, key: str, value):
    conn.execute("""INSERT INTO settings (key, value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                 (key, None if value is None else str(value)))
    conn.commit()


def get_daily_game_limit(conn) -> Optional[int]:
    """一天内最多能玩几款不同的游戏；None = 不限。"""
    v = get_setting(conn, DAILY_GAME_LIMIT)
    return int(v) if v else None


def set_daily_game_limit(conn, n: Optional[int]):
    set_setting(conn, DAILY_GAME_LIMIT, int(n) if n else None)


def get_daily_minutes(conn) -> Optional[float]:
    """一天内所有游戏加起来最多玩多少分钟；None = 不限。"""
    v = get_setting(conn, DAILY_MINUTES)
    return float(v) if v else None


def set_daily_minutes(conn, minutes: Optional[float]):
    set_setting(conn, DAILY_MINUTES, float(minutes) if minutes else None)


def get_daily_minutes_weekend(conn) -> Optional[float]:
    """周末（周六日）的总时长上限；None = 没单独设，周末沿用平日值。"""
    v = get_setting(conn, DAILY_MINUTES_WEEKEND)
    return float(v) if v else None


def set_daily_minutes_weekend(conn, minutes: Optional[float]):
    set_setting(conn, DAILY_MINUTES_WEEKEND, float(minutes) if minutes else None)


def effective_daily_minutes(conn, now_ts: Optional[float] = None) -> Optional[float]:
    """今天实际适用的总时长上限：周末有单独设就用周末档，否则用平日值。

    **只影响上限，不影响口径**——已玩时长照旧按自然日累计（`daily_used_seconds`）。
    跨午夜从周五进周六时上限会变，deadline 每轮重算会跟着变（见 daemon 里 deadline 后移
    重置预警档的处理）。
    """
    from . import rules
    weekday = get_daily_minutes(conn)
    if rules.is_weekend(time.time() if now_ts is None else now_ts):
        return get_daily_minutes_weekend(conn) or weekday
    return weekday


def get_capture_mode(conn) -> str:
    """'manual'（默认）= 只有下单了才采；'auto' = 开游戏就自动采（v0.16.0 之前的行为）。

    老库没有这个键 → 按 manual 走。这是 v0.16.0 的**行为变更**：升级后不点采集就没数据。
    """
    v = get_setting(conn, CAPTURE_MODE)
    return "auto" if v == "auto" else "manual"


def set_capture_mode(conn, mode: str):
    if mode not in ("manual", "auto"):
        raise ValueError(f"unknown capture mode {mode}")
    set_setting(conn, CAPTURE_MODE, mode)


def get_capture_out_dir(conn) -> Optional[str]:
    return get_setting(conn, CAPTURE_OUT_DIR) or None


def set_capture_out_dir(conn, path: Optional[str]):
    set_setting(conn, CAPTURE_OUT_DIR, (path or "").strip() or None)


def games_played_between(conn, start_ts: float, end_ts: float,
                         now_ts: Optional[float] = None) -> dict[int, str]:
    """与 [start, end) 有交集的会话涉及的游戏 {id: 名称}。

    跨午夜的会话两头都算——凌晨 1 点还在玩，那它就占今天一个名额。
    进行中的会话（end_ts 为 NULL）按"到此刻为止"算，否则查未来区间时它会一直命中。
    **观察模式的游戏不计入**——它本就不受任何限制，占名额会把别的游戏挤掉。
    """
    now_ts = time.time() if now_ts is None else now_ts
    rows = conn.execute(
        """SELECT DISTINCT s.game_id, g.name FROM sessions s JOIN games g ON g.id=s.game_id
           WHERE s.start_ts < ? AND COALESCE(s.end_ts, ?) > ?
             AND COALESCE(g.monitor_only, 0) = 0""",
        (int(end_ts), int(now_ts), int(start_ts)))
    return {r["game_id"]: r["name"] for r in rows}


def played_seconds_between(conn, start_ts: float, end_ts: float,
                           now_ts: Optional[float] = None) -> float:
    """[start, end) 区间内的**真实在跑**游玩秒数，跨游戏累加（规则 e 的分母）。

    口径与规则 b 一致：用心跳累计的 `played_seconds`，不是墙钟差——中途退出的时间、
    守护没观测到的空窗期都不算。**观察模式的游戏不计入**（与规则 d 一致：它本就不受
    任何限制，让它吃掉总额会把别的游戏挤没）。

    跨午夜的会话按**墙钟重叠比例分摊**到两天。这是近似——真实游玩秒数没有按分钟落库的
    时间轴，只有一个总数，没法精确切分。误差只发生在"跨零点那一场"，且只有中途退出过
    才会偏；为此加一张逐分钟表不值当。
    """
    now_ts = time.time() if now_ts is None else now_ts
    rows = conn.execute(
        """SELECT s.start_ts, s.end_ts, s.played_seconds
           FROM sessions s JOIN games g ON g.id=s.game_id
           WHERE s.start_ts < ? AND COALESCE(s.end_ts, ?) > ?
             AND COALESCE(g.monitor_only, 0) = 0""",
        (int(end_ts), int(now_ts), int(start_ts)))
    total = 0.0
    for r in rows:
        s_end = r["end_ts"] if r["end_ts"] is not None else now_ts
        span = max(1.0, s_end - r["start_ts"])
        overlap = max(0.0, min(s_end, end_ts) - max(r["start_ts"], start_ts))
        total += session_played(r) * min(1.0, overlap / span)
    return total


def daily_used_seconds(conn, now_ts: Optional[float] = None) -> float:
    """今天（自然日）已经玩掉的总秒数。"""
    from . import rules                     # 延迟导入：rules 依赖 db.Game，顶层导入会成环
    now_ts = time.time() if now_ts is None else now_ts
    return played_seconds_between(conn, *rules.day_bounds(now_ts), now_ts)


def daily_remaining_seconds(conn, now_ts: Optional[float] = None) -> Optional[float]:
    """今天的总时长还剩多少秒；没设规则 e 返回 None（此时**不查库**）。"""
    limit = effective_daily_minutes(conn, now_ts)
    if not limit:
        return None
    return max(0.0, limit * 60 - daily_used_seconds(conn, now_ts))


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
