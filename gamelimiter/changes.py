"""规则变更管制：收紧立即生效，放宽延迟 RELAX_DELAY_HOURS 后生效（防冲动核心）。

GUI 和 CLI 的所有规则修改 / 停用 / 删除都必须走这里，不直接调 db.update_rules。
"""

import json
import time
from typing import Optional

from . import config, db, rules

FIELD_ZH = {"cooldown_hours": "间隔冷却", "session_minutes": "单次最长时长",
            "windows": "允许时段", "enabled": "启用状态", "__delete__": "删除游戏"}


def is_tightening(field: str, old, new) -> bool:
    """new 相对 old 是否为收紧（含不变）。收紧立即生效，放宽入待生效队列。"""
    if field == "cooldown_hours":
        return (new or 0) >= (old or 0)                    # 冷却更长 = 更严
    if field == "session_minutes":
        inf = float("inf")
        return (new or inf) <= (old or inf)                # 单次更短 = 更严；None = 不限
    if field == "windows":
        return rules.coverage(new) <= rules.coverage(old)  # 可玩时间是子集 = 更严
    if field == "enabled":
        return bool(new) >= bool(old)                      # 启用 = 更严；停用 = 放宽
    raise ValueError(field)


def _norm(field: str, v):
    if field == "enabled":
        return int(bool(v))
    if field == "windows":
        return sorted(v) if v else None
    return v or None


def request_changes(conn, g: db.Game, fields: dict) -> tuple[dict, list]:
    """申请一组规则变更。返回 (立即生效的 {field: value}, 延迟的 [(field, value, apply_at)])。"""
    applied, delayed = {}, []
    for f, v in fields.items():
        old = getattr(g, f) if f != "enabled" else int(g.enabled)
        if _norm(f, v) == _norm(f, old):
            continue
        if is_tightening(f, old, v):
            applied[f] = v
            db.clear_pending_field(conn, g.id, f)   # 改主意变严 → 撤销之前的放宽申请
        else:
            apply_at = int(time.time() + config.RELAX_DELAY_HOURS * 3600)
            db.upsert_pending(conn, g.id, f, v, apply_at)
            delayed.append((f, v, apply_at))
    if applied:
        db.update_rules(conn, g.id, **applied)
    return applied, delayed


def request_delete(conn, g: db.Game) -> Optional[int]:
    """申请删除。未受限游戏立即删（返回 None）；受限游戏延迟删（返回 apply_at）。"""
    restricted = g.enabled and (g.cooldown_hours or g.session_minutes or g.windows)
    if not restricted:
        db.remove_game(conn, g.id)
        return None
    apply_at = int(time.time() + config.RELAX_DELAY_HOURS * 3600)
    db.upsert_pending(conn, g.id, "__delete__", None, apply_at)
    return apply_at


# ---- 本次游玩额度（规则 b 的一次性收紧）----

def set_next_session(conn, g: db.Game, minutes) -> tuple[bool, str]:
    """设置下次会话的一次性额度（分钟）；None / 0 = 清除，回到上限。

    额度只能 ≤ 上限，永远不构成放宽 → 立即生效，不进待生效队列。
    放宽上限本身仍走 request_changes 的 24h 延迟。
    """
    minutes = float(minutes) if minutes else None
    if minutes is not None:
        if minutes <= 0:
            minutes = None
        elif g.session_minutes and minutes > g.session_minutes:
            return False, f"本次额度不能超过单次最长 {g.session_minutes:g} 分钟"
    if minutes == g.next_session_minutes:
        return True, ""
    db.set_next_session(conn, g.id, minutes)
    db.log_event(conn, g.id, "quota", f"next={minutes:g}min" if minutes else "next=cleared")
    if minutes is None:
        cap = f"{g.session_minutes:g} 分钟" if g.session_minutes else "不限"
        return True, f"已清除本次额度，下次按上限（{cap}）"
    return True, f"下次游玩限 {minutes:g} 分钟"


def shorten_running_session(conn, g: db.Game, sess, minutes,
                            now: Optional[float] = None) -> tuple[bool, str]:
    """改进行中会话的额度：**只许缩短**——玩到一半想加时正是要拦的冲动。

    下限 = 已玩 + 最长预警档：缩短后仍要收得到预警，PVP 被无预警强杀会判逃跑。
    """
    now = now or time.time()
    minutes = float(minutes) if minutes else None
    if minutes is None:
        return False, "游玩中不能取消本次额度，只能缩短"
    cur = rules.effective_limit(g.session_minutes, sess["limit_minutes"])
    if cur and minutes >= cur:
        return False, f"游玩中只能缩短本次时长（当前 {cur:g} 分钟），不能加时"
    played = (now - sess["start_ts"]) / 60
    buffer = max(config.WARN_MINUTES)
    if minutes < played + buffer:
        return False, (f"本次已玩 {played:.0f} 分钟，需留 {buffer:g} 分钟预警缓冲，"
                       f"最短可设 {played + buffer:.0f} 分钟")
    db.set_session_limit(conn, sess["id"], minutes)
    db.log_event(conn, g.id, "quota", f"session={minutes:g}min")
    return True, f"本次游玩缩短到 {minutes:g} 分钟"


def cancel_pending(conn, pending_id: int):
    """取消待生效的放宽（保持更严的现状，随时允许）。"""
    db.delete_pending(conn, pending_id)


def apply_due(conn, now: float) -> int:
    """把到期的待生效变更落地（守护进程周期调用）。返回应用条数。"""
    n = 0
    for p in db.due_pendings(conn, now):
        if p["field"] == "__delete__":
            db.remove_game(conn, p["game_id"])
        else:
            v = json.loads(p["value"])["v"]
            db.update_rules(conn, p["game_id"], **{p["field"]: v})
            db.delete_pending(conn, p["id"])
        n += 1
    return n


def describe_pending(p) -> str:
    """给 UI/CLI 的一行中文描述。"""
    from datetime import datetime
    t = datetime.fromtimestamp(p["apply_at"]).strftime("%m-%d %H:%M")
    if p["field"] == "__delete__":
        return f"解除全部限制并删除，{t} 生效"
    v = json.loads(p["value"])["v"]
    if p["field"] == "enabled":
        desc = "停用限制"
    elif p["field"] == "windows":
        desc = f"允许时段改为 {'、'.join(v) if v else '不限'}"
    elif p["field"] == "cooldown_hours":
        desc = f"冷却改为 {f'{v:g} 小时' if v else '无'}"
    else:
        desc = f"单次最长改为 {f'{v:g} 分钟' if v else '不限'}"
    return f"{desc}，{t} 生效"
