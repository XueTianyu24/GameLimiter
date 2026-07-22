"""变更管制单元测试：收紧/放宽分类 + 延迟落地。

跑法：conda run -n gamelimiter python tests/test_changes.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tempfile

from gamelimiter import config

config.DB_PATH = Path(tempfile.mkdtemp()) / "test.db"   # 隔离测试库（须在 db.connect 前）

from gamelimiter import changes, db

# ---- 分类 ----
T = changes.is_tightening
assert T("cooldown_hours", None, 4)          # 加冷却 = 收紧
assert T("cooldown_hours", 2, 4)
assert not T("cooldown_hours", 4, 2)         # 减冷却 = 放宽
assert not T("cooldown_hours", 4, None)
assert T("session_minutes", None, 90)        # 加时长上限 = 收紧
assert T("session_minutes", 90, 60)
assert not T("session_minutes", 60, 90)
assert not T("session_minutes", 90, None)
assert T("windows", None, ["19:00-23:00"])   # 加时段限制 = 收紧
assert T("windows", ["19:00-23:00"], ["20:00-22:00"])        # 子集 = 收紧
assert not T("windows", ["20:00-22:00"], ["19:00-23:00"])    # 超集 = 放宽
assert not T("windows", ["19:00-23:00"], None)
assert not T("windows", ["19:00-23:00"], ["12:00-13:00"])    # 换到别的时段也算放宽（新增可玩分钟）
assert T("windows", ["22:00-01:00"], ["22:00-00:00"])        # 跨午夜子集
assert T("enabled", 0, 1)                    # 启用 = 收紧
assert not T("enabled", 1, 0)                # 停用 = 放宽

# ---- 落库流程 ----
conn = db.connect()
g = db.upsert_game(conn, "测试", "t.exe", cooldown_hours=4, session_minutes=90)

applied, delayed = changes.request_changes(conn, g, {"cooldown_hours": 6, "session_minutes": 120})
assert list(applied) == ["cooldown_hours"] and len(delayed) == 1   # 收紧即生效，放宽入队
g = db.get_game(conn, "t.exe")
assert g.cooldown_hours == 6 and g.session_minutes == 90
assert len(db.list_pending(conn, g.id)) == 1

# 改主意变严 → 撤销放宽申请
applied, delayed = changes.request_changes(conn, g, {"session_minutes": 60})
assert applied == {"session_minutes": 60} and not delayed
assert not db.list_pending(conn, g.id)

# 未到期不落地；到期落地
changes.request_changes(conn, db.get_game(conn, "t.exe"), {"session_minutes": 120})
assert changes.apply_due(conn, time.time()) == 0
assert changes.apply_due(conn, time.time() + 25 * 3600) == 1
assert db.get_game(conn, "t.exe").session_minutes == 120

# 受限游戏删除 = 延迟；到期真删
g = db.get_game(conn, "t.exe")
apply_at = changes.request_delete(conn, g)
assert apply_at is not None and db.get_game(conn, "t.exe") is not None
changes.apply_due(conn, apply_at + 1)
assert db.get_game(conn, "t.exe") is None

# 未受限游戏删除 = 立即
g2 = db.upsert_game(conn, "无规则", "t2.exe")
assert changes.request_delete(conn, g2) is None
assert db.get_game(conn, "t2.exe") is None

print("test_changes: 全部通过")
