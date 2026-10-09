# -*- coding: utf-8 -*-
"""
幸运大转盘 —— 零依赖抽奖系统
--------------------------------------------------
技术栈：Python 3.8+ 标准库（http.server + sqlite3），无需 pip install 任何东西。

两个页面：
    http://<host>:<port>/          用户端（登录 -> 转盘抽奖 -> 中奖填收货信息）
    http://<host>:<port>/admin     管理端（默认密码 admin888）

核心约束（永久一次）：
    1) records 表对 uid 建立 UNIQUE 约束；
    2) 抽奖走 BEGIN IMMEDIATE 写事务 + 进程内写锁，双保险；
    3) 不提供任何"删除抽奖记录"的接口 —— 抽过就是抽过，刷新/清缓存/换设备都无法重来。
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import queue
import random
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------
# 常量与路径
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 允许用环境变量把数据目录挪走（自动化测试用，避免污染真实库）
DATA_DIR = os.environ.get("LOTTERY_DATA_DIR") or os.path.join(BASE_DIR, "data")
TPL_DIR = os.path.join(BASE_DIR, "templates")
DB_PATH = os.path.join(DATA_DIR, "lottery.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULT_PASSWORD = "admin888"
DEFAULT_TITLE = "幸运大转盘"
NO_WIN_NAME = "谢谢参与"
APP_VERSION = "3.1.0"

# 多奖品独立抽奖：每个奖品在 prizes 表里各有一份 库存 / 物料编号，
# 转盘上**每个奖品一块扇区**、「谢谢参与」穿插其间，全部等分。
#
# ⚠️ 扇区大小**不代表中奖概率** —— 视觉与概率是解耦的：
#    扇区只决定「停下来时指针指向谁」，真实中奖率由 dynamic_win_rate() 算：
#        中奖率 = 剩余礼品数 ÷ 剩余未抽人数
#    于是礼品会均匀铺满整场活动，既不会「前 20 个人把礼品抽光」，
#    也基本不会到最后还剩一堆发不完。

# 测试用户标记：只有显式标记为测试的用户，抽奖才不消耗库存、不进正式统计。
# 刻意不按 ID 长度/前缀来判断 —— 正式 ID 也可能是 3 位，靠格式猜必然误伤。
TEST_BATCH_TAG_SETTING = "test_batch_tag"   # 管理端批量导入测试ID时写入的批次号

# 发货表（导出）固定字段，可在管理端「系统设置」里改
DEPT_CODE = "1000003263"
ORDER_TITLE = "双节课消活动礼品"
ORDER_NO = "双节课消活动礼品"

REGIONS_PATH = os.path.join(BASE_DIR, "regions.json")
_REGIONS = {"data": None, "etag": ""}

WRITE_LOCK = threading.Lock()   # 串行化所有写操作
NO_WIN_SECTOR = {
    "prize_id": None, "name": NO_WIN_NAME, "prob": 100.0, "is_win": 0,
    "angle": 360.0, "start": -90.0, "end": 270.0,
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()


_CFG_CACHE = {"path": None, "cfg": None}
_CFG_LOCK = threading.Lock()


def load_config(create: bool = True):
    """读配置。进程内缓存 —— 每个鉴权请求都要用它（校验签名/密码），
    反复读盘 + json.loads 是单请求最大开销之一，缓存后降为一次字典查表。
    """
    hit = _CFG_CACHE["cfg"]
    if hit is not None and _CFG_CACHE["path"] == CONFIG_PATH:
        return hit
    with _CFG_LOCK:
        hit = _CFG_CACHE["cfg"]
        if hit is not None and _CFG_CACHE["path"] == CONFIG_PATH:
            return hit
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                cfg.setdefault("secret_key", secrets.token_hex(16))
                _CFG_CACHE["cfg"] = cfg
                _CFG_CACHE["path"] = CONFIG_PATH
                return cfg
            except Exception:
                pass
        if not create:
            return None
        salt = secrets.token_hex(16)
        cfg = {
            "salt": salt,
            "password_hash": _hash_pw(DEFAULT_PASSWORD, salt),
            "secret_key": secrets.token_hex(16),
            "created_at": now_str(),
        }
        save_config(cfg)
        return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    _CFG_CACHE["cfg"] = cfg           # 写回缓存，避免下一次请求再读盘
    _CFG_CACHE["path"] = CONFIG_PATH


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def region_province_count():
    """regions.json 里的省级数量（启动时读一次，用于自检与设置页展示）。"""
    try:
        with open(REGIONS_PATH, "r", encoding="utf-8") as f:
            return len(json.load(f))
    except Exception:
        return 0


REGION_PROVINCE_COUNT = region_province_count()


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
# 连接池：sqlite3.connect() 本身只要 ~3ms，但**在这条连接上执行第一条真正的读语句**
# 会额外付出约 26ms（打开库文件、加载页缓存、解析模式；这段在 GIL 之外执行，
# 所以 600 个并发请求会各扛一次 26ms —— 表现为"并发一上来每个请求都要 30ms+ 才响应"）。
# 于是不再每次请求新建连接，改为复用「已预热」的连接池：
#   · 连接创建后先跑一次预热查询，之后这条连接上所有查询都在亚毫秒级；
#   · 借出/归还由 _POOL_LOCK 保护，池空则新建（不阻塞请求）；
#   · check_same_thread=False：连接会在线程池里被不同工作线程轮流借用。
#     安全前提是「同一时刻只有一条线程持有某条连接」+「所有写事务都由 WRITE_LOCK 串行化」，
#     本文件严格满足这两点（每次 get_db 必须配对 put_db，绝无跨线程共享）。
_POOL = []
_POOL_LOCK = threading.Lock()
POOL_MAX_IDLE = int(os.environ.get("DB_POOL_MAX", "64"))
POOL_PREWARM = int(os.environ.get("DB_POOL_PREWARM", "24"))


def _new_conn():
    # isolation_level=None => 自动提交模式，事务由我们显式 BEGIN/COMMIT 控制，避免隐式事务冲突
    con = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # journal_mode 是「数据库文件的持久属性」，设一次就永久生效。
    # 早期版本每次连接都执行 PRAGMA journal_mode=WAL，会触发文件头读写+同步
    #（实测单次约 12ms），是本请求路径上的第二大热点。现在只在启动时设一次。
    con.execute("PRAGMA busy_timeout=10000")
    # ↓ 关键预热：把库文件/页缓存真正加载进来，之后这条连接上的查询从 ~26ms 降到 ~0.02ms。
    #   用 sqlite_master（任何库都有），这样建库前的第一条连接也能安全预热。
    con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return con


def get_db():
    """借一条（已预热的）连接；用完必须 put_db() 归还。"""
    while True:
        with _POOL_LOCK:
            con = _POOL.pop() if _POOL else None
        if con is None:
            return _new_conn()          # 池空：新建（已内含预热）
        try:
            con.execute("SELECT 1")     # 探活：工作线程借到的连接可能上一轮刚归还
            return con
        except Exception:
            try:
                con.close()
            except Exception:
                pass
            # 这条坏了，丢掉继续换一条
            continue


def put_db(con):
    """归还连接到池。坏连接、或池已满则直接关闭。"""
    if con is None:
        return
    try:
        con.execute("SELECT 1")          # 探活
    except Exception:
        try:
            con.close()
        except Exception:
            pass
        return
    with _POOL_LOCK:
        if len(_POOL) >= POOL_MAX_IDLE:
            try:
                con.close()
            except Exception:
                pass
        else:
            _POOL.append(con)


def _prewarm_pool(n=None):
    """启动时预建 n 条连接并预热，让第一批用户直接命中热连接。"""
    n = POOL_PREWARM if n is None else n
    conns = []
    for _ in range(max(0, n)):
        try:
            conns.append(_new_conn())
        except Exception:
            break
    with _POOL_LOCK:
        _POOL.extend(conns)
    return len(conns)


_WAL_DONE = {"path": None}


def _set_wal_mode_once():
    """把数据库切到 WAL（幂等）。只在进程启动时跑一次。

    ⚠️ 必须靠进程内标记拦住：即使 journal_mode 已经是 wal，
    再执行一次 `PRAGMA journal_mode=WAL` 仍会强制做一次 WAL checkpoint，
    实测单次约 55ms —— 比原来「每次连接都设」还慢。
    """
    if _WAL_DONE["path"] == DB_PATH:
        return
    con = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")   # WAL 下 NORMAL 已足够安全，显著减少 fsync
    finally:
        con.close()
    _WAL_DONE["path"] = DB_PATH


# 首次建库时的初始奖品。按需求「奖品表清空、由后台自己配」，这里默认不给任何奖品；
# 建好库后在管理端「奖品与概率」里逐条添加即可（名称 / 物料编号 / 概率 / 库存 都可改）。
DEFAULT_PRIZES = []


SCHEMA_VERSION = 3   # 表结构代号；库里的 settings.schema_version == 此值时可跳过全部迁移检查


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    _set_wal_mode_once()
    con = get_db()
    # 快速路径：结构已是最新则直接返回。
    # 上面那句 executescript(4 张表 + 3 个索引) 与 8 次 PRAGMA table_info 实测约 16ms，
    # 重启一次无妨，但在「每次请求顺手 init」的旧代码路径里会被放大成热点。
    try:
        if to_int(setting(con, "schema_version", "0"), 0) >= SCHEMA_VERSION:
            put_db(con)
            return
    except Exception:
        pass
    # 1) 建表（不动已有表）
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            uid      TEXT PRIMARY KEY,
            added_at TEXT,
            is_test  INTEGER NOT NULL DEFAULT 0,   -- 1 = 测试ID：抽奖不消耗库存、不进正式统计、发货表默认排除
            tag      TEXT NOT NULL DEFAULT ''      -- 标记来源，例如 admin / test:20261008
        );
        CREATE TABLE IF NOT EXISTS prizes (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL,
            code  TEXT NOT NULL DEFAULT '',       -- 物料编号，导出「发货表」E 列用
            prob  REAL NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT -1,
            sort  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS records (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         TEXT NOT NULL UNIQUE,      -- 永久一次，数据库级保证
            prize_id    INTEGER,
            prize_name  TEXT,
            is_win      INTEGER NOT NULL DEFAULT 0,
            draw_at     TEXT,
            name        TEXT,
            phone       TEXT,
            province    TEXT DEFAULT '',           -- 省 / 市 / 区县：用户端三级下拉框
            city        TEXT DEFAULT '',
            district    TEXT DEFAULT '',
            address     TEXT,                      -- 详细地址（门牌）
            prize_code  TEXT DEFAULT '',           -- 中奖时的物料编号快照
            gift_id     INTEGER,                   -- 用户主动选择的礼品（实物周边模式）
            gift_name   TEXT DEFAULT '',           -- 选礼品时的名字快照
            gift_code   TEXT DEFAULT '',           -- 选礼品时的物料编号快照 ← 发货表 E 列用这个
            is_test     INTEGER NOT NULL DEFAULT 0,-- 测试记录：不计入库存消耗、发货表默认排除
            shipping_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        """
    )
    # 2) 老版本数据库补列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
    #    ⚠️ 必须排在「建索引」之前：老库的 records 表还没有 gift_id，
    #    如果先 CREATE INDEX ON records(gift_id) 就会报 "no such column: gift_id" 起不来。
    for table, spec in (
        ("users", [("is_test", "INTEGER NOT NULL DEFAULT 0"), ("tag", "TEXT NOT NULL DEFAULT ''")]),
        ("prizes", [("code", "TEXT NOT NULL DEFAULT ''")]),
        ("records", [("province", "TEXT DEFAULT ''"), ("city", "TEXT DEFAULT ''"),
                     ("district", "TEXT DEFAULT ''"), ("prize_code", "TEXT DEFAULT ''"),
                     ("gift_id", "INTEGER"), ("gift_name", "TEXT DEFAULT ''"),
                     ("gift_code", "TEXT DEFAULT ''"),
                     ("is_test", "INTEGER NOT NULL DEFAULT 0")]),
    ):
        have = {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}
        for col, decl in spec:
            if col not in have:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))

    # 3) 建索引（放在补列之后，保证 gift_id 一定存在）
    con.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_records_draw  ON records(draw_at);
        CREATE INDEX IF NOT EXISTS idx_records_prize ON records(prize_id);
        CREATE INDEX IF NOT EXISTS idx_records_gift  ON records(gift_id);
        """
    )

    # v2 → v3：从「单一奖项 + 候选礼品」改回「多奖品独立抽奖」。
    # 旧的候选礼品没有独立概率/库存，已不适用；且需求明确要求奖品由后台自己配，
    # 所以这里清空一次（只在首次升到 v3 时执行，之后 schema_version=3 走快速路径跳过）。
    if to_int(setting(con, "schema_version", "0"), 0) < 3:
        con.execute("DELETE FROM prizes")

    if con.execute("SELECT COUNT(*) c FROM prizes").fetchone()["c"] == 0:
        for i, (name, code, prob, stock) in enumerate(DEFAULT_PRIZES):
            con.execute("INSERT INTO prizes(name, code, prob, stock, sort) VALUES (?,?,?,?,?)",
                        (name, code, prob, stock, i))
    defaults = {"whitelist_only": "1", "title": DEFAULT_TITLE,
                "dept_code": DEPT_CODE, "order_title": ORDER_TITLE, "order_no": ORDER_NO}
    for k, v in defaults.items():
        con.execute("INSERT OR IGNORE INTO settings(k, v) VALUES (?,?)", (k, v))
    set_setting(con, "schema_version", SCHEMA_VERSION)
    con.commit()
    put_db(con)


def setting(con, key, default=""):
    row = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(con, key, value):
    con.execute("INSERT INTO settings(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)))


# --------------------------------------------------------------------------
# 业务逻辑
# --------------------------------------------------------------------------
def normalize_uid(raw):
    if raw is None:
        return ""
    uid = str(raw).strip().replace("\u3000", " ")
    uid = "".join(uid.split())
    uid = uid.replace(",", "").replace("，", "")
    if len(uid) > 64:
        uid = uid[:64]
    return uid


def parse_id_list(text):
    """从粘贴文本/CSV 里解析出 ID 列表，保序去重。"""
    if not text:
        return []
    text = text.replace("，", ",").replace("、", ",")
    out, seen = [], set()
    for line in text.splitlines():
        for token in line.split(","):
            for piece in token.split():
                uid = normalize_uid(piece)
                if uid and uid not in seen:
                    seen.add(uid)
                    out.append(uid)
    return out


def to_int(v, default=-1):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def prize_stock_used(con):
    """每个奖品已经发出去的数量。

    只统计**正式记录**：测试记录不占正式库存，所以测试ID怎么抽都不会把奖品抽光
    （这正是「测试ID不影响真实库存」那条需求要的效果）。
    """
    rows = con.execute(
        "SELECT prize_id, COUNT(*) c FROM records "
        "WHERE is_win=1 AND COALESCE(is_test,0)=0 AND prize_id IS NOT NULL "
        "GROUP BY prize_id").fetchall()
    return {r["prize_id"]: int(r["c"]) for r in rows}


def list_prizes(con):
    """奖品清单（管理端配置用）：带已发放数量与剩余库存。"""
    used = prize_stock_used(con)
    out = []
    for r in con.execute("SELECT * FROM prizes ORDER BY sort, id").fetchall():
        stock = to_int(r["stock"], -1)
        u = used.get(r["id"], 0)
        out.append({
            "id": r["id"], "name": r["name"], "code": (r["code"] or ""),
            "prob": float(r["prob"] or 0), "stock": stock, "sort": int(r["sort"] or 0),
            "used": u,
            "left": ("不限" if stock < 0 else max(stock - u, 0)),
            "sold_out": 1 if (stock >= 0 and u >= stock) else 0,
        })
    return out


def _no_win_sector():
    return {"prize_id": None, "name": NO_WIN_NAME, "code": "", "is_win": 0,
            "stock": -1, "used": 0, "left": -1, "live": False}


def left_people_count(con, is_test=False):
    """名单里**还没抽过奖**的人数（也就是「还会来抽的人」）。

    用 LEFT JOIN 数「没有记录的用户」，而不是「总人数 − 记录数」——
    后者在名单被换过、或库里存着名单外 ID 的记录时会算歪（甚至算成负数），
    进而把中奖率错误地压成 0。
    """
    return con.execute(
        "SELECT COUNT(*) c FROM users u LEFT JOIN records r ON r.uid = u.uid "
        "WHERE COALESCE(u.is_test,0)=? AND r.id IS NULL",
        (1 if is_test else 0,)).fetchone()["c"]


def dynamic_win_rate(con, is_test=False):
    """动态中奖率 = 剩余礼品数 ÷ 剩余未抽人数。

    这是「扇区与概率解耦」方案的核心：把有限的礼品**摊平**到整场活动里 ——
      · 前面运气好、中得多 → 剩余礼品变少，概率自动回落；
      · 前面没中出去 → 概率自动抬升。
    于是既不会「前 20 个人把礼品抽光、后面 480 人白参与」，
    也基本不会出现到最后还剩一堆发不完。

    测试ID：分母用测试号人数、分子用**配置的总库存**（测试抽奖不消耗正式库存，
    所以对它来说永远有货），方便反复联调。
    """
    prizes = list_prizes(con)
    left_people = left_people_count(con, is_test=is_test)

    if is_test:
        total = sum(max(0, p["stock"]) for p in prizes if p["stock"] >= 0)
        if total <= 0:
            return min(1.0, float(setting(con, "fixed_win_prob", "10") or 10) / 100.0)
        if left_people <= 0:
            return 0.0
        return min(1.0, float(total) / left_people)

    left = sum(max(0, p["stock"] - p["used"]) for p in prizes if p["stock"] >= 0)
    if left <= 0:
        # 没有「有限库存」的奖品了：可能全是不限量，也可能确实都发完了。
        # 前者退回固定概率（否则中奖率会变成 0，等于抽不到），后者就是真的没得发了。
        if any(p["stock"] < 0 for p in prizes):
            return min(1.0, float(setting(con, "fixed_win_prob", "10") or 10) / 100.0)
        return 0.0
    if left_people <= 0:
        return 0.0                # 名单里的人都抽完了，不再发放
    return min(1.0, float(left) / left_people)


def pick_prize(con, is_test=False):
    """中奖后挑一件礼品：**按剩余库存加权**。

    剩得多的更容易被抽中，各礼品于是自动趋于同时发完，
    不会出现「A 早就发完了、B 还堆着没人要」。

    测试ID（is_test）不受库存限制：某个奖品即使已经发完，它照样抽得到
    （权重退回该奖品的配置库存），这样正式库存清空后还能继续联调。
    """
    pool = []
    for p in list_prizes(con):
        if is_test:
            w = float(p["stock"]) if p["stock"] > 0 else 1.0
        elif p["stock"] < 0:
            w = 1.0                     # 不限量
        else:
            w = float(p["left"])
        if w > 0:
            pool.append((p, w))
    if not pool:
        return None
    total = sum(w for _, w in pool)
    r = random.uniform(0, total)
    acc = 0.0
    for p, w in pool:
        acc += w
        if r <= acc:
            return p
    return pool[-1][0]


def build_sectors(con, is_test=False):
    """转盘扇区：**每个奖品一块 + 「谢谢参与」若干块，全部等分**。

    ⚠️ 扇区大小**不再代表中奖概率** —— 视觉与概率是解耦的：
    扇区只决定「停下来时指针指向谁」，真实中奖率由 dynamic_win_rate() 算。
    所以哪怕中奖率只有 2%，扇区也能画得又大又清楚。

    「谢谢参与」和奖品**交替排列**（各占一半），视觉上不会出现
    「一整块巨大的谢谢参与 + 几个看不清的小米粒」—— 商业转盘的常规做法。

    库存发完的奖品**照旧留在盘上、名字照常显示**，只是不会再被抽中；
    刻意不给前端任何「已抢完」标记，用户看不出区别。
    """
    prizes = list_prizes(con)
    if not prizes:
        entries = [_no_win_sector()]
    else:
        interleave = len(prizes) <= 6      # 奖品不多就交替排，多了收敛成一块
        entries = []
        for p in prizes:
            entries.append({"prize_id": p["id"], "name": p["name"], "code": p["code"],
                            "is_win": 1, "stock": p["stock"], "used": p["used"],
                            "left": p["left"],
                            "live": bool(is_test or p["stock"] < 0 or p["left"] > 0)})
            if interleave:
                entries.append(_no_win_sector())
        if not interleave:
            entries.append(_no_win_sector())

    n = len(entries)
    deg = 360.0 / n
    start = -90.0
    for e in entries:
        e["angle"] = deg
        e["start"] = start
        e["end"] = start + deg
        e["prob"] = round(100.0 / n, 4)    # 只表示「视觉占比」，不代表中奖率
        start += deg
    return entries


def public_sectors(sectors):
    """给前端的扇区：**剥掉库存内部字段**，免得把「这个奖品发完了」暴露到浏览器里。"""
    return [{"prize_id": s.get("prize_id"), "name": s["name"], "prob": s["prob"],
             "is_win": s["is_win"], "angle": s["angle"], "start": s["start"], "end": s["end"]}
            for s in sectors]


def record_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["is_win"] = int(d.get("is_win") or 0)
    d["is_test"] = int(d.get("is_test") or 0)
    d["shipping_done"] = bool(d.get("shipping_at"))
    # 发货表 E 列用的编号 = 中奖那个奖品自己的物料编号（中奖时已存快照）
    d["code_use"] = (d.get("prize_code") or "").strip()
    return d


def get_record(con, uid):
    return con.execute("SELECT * FROM records WHERE uid=?", (uid,)).fetchone()


def is_test_uid(con, uid):
    row = con.execute("SELECT is_test FROM users WHERE uid=?", (uid,)).fetchone()
    return bool(row and int(row["is_test"] or 0) == 1)


def stats(con):
    """看板数据。测试记录单独计数，**不混进正式统计**，免得误判库存消耗。"""
    R = "COALESCE(is_test,0)=0"
    total_users = con.execute("SELECT COUNT(*) c FROM users WHERE COALESCE(is_test,0)=0").fetchone()["c"]
    test_users = con.execute("SELECT COUNT(*) c FROM users WHERE COALESCE(is_test,0)=1").fetchone()["c"]
    drawn = con.execute("SELECT COUNT(*) c FROM records WHERE " + R).fetchone()["c"]
    test_drawn = con.execute("SELECT COUNT(*) c FROM records WHERE COALESCE(is_test,0)=1").fetchone()["c"]
    won = con.execute("SELECT COUNT(*) c FROM records WHERE is_win=1 AND " + R).fetchone()["c"]
    pending = con.execute(
        "SELECT COUNT(*) c FROM records WHERE is_win=1 AND " + R +
        " AND (shipping_at IS NULL OR shipping_at='')"
    ).fetchone()["c"]
    per = con.execute(
        "SELECT COALESCE(NULLIF(TRIM(prize_name),''), ?) n, COUNT(*) c FROM records "
        "WHERE is_win=1 AND " + R + " GROUP BY prize_name ORDER BY c DESC",
        (NO_WIN_NAME,),
    ).fetchall()
    prizes = list_prizes(con)
    return {
        "total_users": total_users,
        "drawn": drawn,
        "won": won,
        "no_win": drawn - won,
        "pending_shipping": pending,
        "undrawn": max(total_users - drawn, 0),
        "per_prize": [{"name": r["n"], "count": r["c"]} for r in per],
        # 奖品维度：每个奖品的库存 / 已发 / 剩余
        "prizes": prizes,
        "prize_count": len(prizes),
        "no_code": sum(1 for p in prizes if not (p["code"] or "").strip()),
        # 动态中奖率（剩余礼品 ÷ 剩余人数）—— 会随活动进度自动变化
        "win_rate": round(dynamic_win_rate(con) * 100.0, 4),
        "pool_stock": sum(max(0, p["stock"] - p["used"]) for p in prizes if p["stock"] >= 0),
        "whitelist_only": setting(con, "whitelist_only", "1") == "1",
        "title": setting(con, "title", DEFAULT_TITLE),
        "shipping_filled": won - pending,
        "test_users": test_users,
        "test_drawn": test_drawn,
    }


# --------------------------------------------------------------------------
# 管理员令牌
# --------------------------------------------------------------------------
def make_token(cfg, hours=12):
    exp = int(time.time()) + hours * 3600
    payload = json.dumps({"r": "admin", "exp": exp}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(cfg["secret_key"].encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return body + "." + sig


def check_token(cfg, token):
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    good = hmac.new(cfg["secret_key"].encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good):
        return False
    try:
        pad = "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(body + pad))
    except Exception:
        return False
    return float(data.get("exp", 0)) > time.time()


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "LuckyWheel/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now_str(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_ok(self, data=None, **kw):
        payload = {"ok": True}
        if data:
            payload.update(data)
        payload.update(kw)
        self._send(200, json.dumps(payload, ensure_ascii=False))

    def json_err(self, msg, code=400):
        self._send(code, json.dumps({"ok": False, "error": msg}, ensure_ascii=False))

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > 8 * 1024 * 1024:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def cookies(self):
        out = {}
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def is_admin(self):
        cfg = load_config()
        return check_token(cfg, self.cookies().get("admin_token", ""))

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html", "/user"):
            return self.serve_template("user.html")
        if path in ("/admin", "/admin/", "/admin.html"):
            return self.serve_template("admin.html")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/api/health":
            return self.json_ok({"time": now_str()})

        if path == "/api/regions":
            return self.serve_regions()

        if path == "/api/public/settings":
            con = get_db()
            try:
                return self.json_ok({"title": setting(con, "title", DEFAULT_TITLE),
                                     "whitelist_only": setting(con, "whitelist_only", "1") == "1"})
            finally:
                put_db(con)

        if path == "/api/user/prizes":
            con = get_db()
            try:
                return self.json_ok({"sectors": public_sectors(build_sectors(con)),
                                     "title": setting(con, "title", DEFAULT_TITLE)})
            finally:
                put_db(con)

        if path.startswith("/api/admin/"):
            if not self.is_admin():
                return self.json_err("未登录或登录已过期", 401)
            return self.admin_get(path, qs)
        return self.json_err("接口不存在", 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self.read_json()
        try:
            if path == "/api/admin/login":
                return self.api_admin_login(body)
            if path == "/api/admin/logout":
                return self._send(200, json.dumps({"ok": True}), "application/json; charset=utf-8",
                                  {"Set-Cookie": "admin_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
            if path == "/api/user/login":
                return self.api_user_login(body)
            if path == "/api/user/spin":
                return self.api_user_spin(body)
            if path == "/api/user/shipping":
                return self.api_user_shipping(body)
            if path.startswith("/api/admin/"):
                if not self.is_admin():
                    return self.json_err("未登录或登录已过期", 401)
                return self.admin_post(path, body)
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            return self.json_err("服务器错误：%s" % e, 500)
        return self.json_err("接口不存在", 404)

    def serve_regions(self):
        """全国省/市/区县数据（regions.json），带 ETag，浏览器只拉一次。"""
        if not os.path.exists(REGIONS_PATH):
            return self.json_err("行政区划数据文件缺失：regions.json", 500)
        if _REGIONS["data"] is None:
            with open(REGIONS_PATH, "rb") as f:
                _REGIONS["data"] = f.read()
            _REGIONS["etag"] = '"%s"' % hashlib.md5(_REGIONS["data"]).hexdigest()[:16]
        etag = _REGIONS["etag"]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(_REGIONS["data"])))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(_REGIONS["data"])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_template(self, name):
        fp = os.path.join(TPL_DIR, name)
        if not os.path.exists(fp):
            return self.json_err("模板缺失：%s" % name, 500)
        with open(fp, "rb") as f:
            data = f.read()
        self._send(200, data, "text/html; charset=utf-8")

    # ---------- 用户端 ----------
    def api_user_login(self, body):
        uid = normalize_uid(body.get("uid"))
        if not uid:
            return self.json_err("请填写用户ID")
        con = get_db()
        try:
            wl_only = setting(con, "whitelist_only", "1") == "1"
            exists = con.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone()
            if wl_only and not exists:
                return self.json_ok({"status": "not_found", "uid": uid,
                                     "message": "该ID不在参与名单中，请联系运营人员"})
            rec = get_record(con, uid)
            if rec:
                r = record_to_dict(rec)
                if r["is_win"] and not r["shipping_done"]:
                    return self.json_ok({"status": "need_shipping", "uid": uid, "record": r,
                                         "title": setting(con, "title", DEFAULT_TITLE)})
                return self.json_ok({"status": "already_drawn", "uid": uid, "record": r,
                                     "title": setting(con, "title", DEFAULT_TITLE)})
            return self.json_ok({"status": "ok", "uid": uid,
                                 "sectors": public_sectors(
                                     build_sectors(con, is_test=is_test_uid(con, uid))),
                                 "title": setting(con, "title", DEFAULT_TITLE)})
        finally:
            put_db(con)

    def api_user_spin(self, body):
        uid = normalize_uid(body.get("uid"))
        if not uid:
            return self.json_err("请填写用户ID")
        with WRITE_LOCK:
            con = get_db()
            try:
                con.execute("BEGIN IMMEDIATE")
                wl_only = setting(con, "whitelist_only", "1") == "1"
                if wl_only:
                    if not con.execute("SELECT 1 FROM users WHERE uid=?", (uid,)).fetchone():
                        con.rollback()
                        return self.json_ok({"status": "not_found", "message": "该ID不在参与名单中"})
                rec = get_record(con, uid)
                if rec:
                    con.rollback()
                    r = record_to_dict(rec)
                    return self.json_ok({"status": "need_shipping" if (r["is_win"] and not r["shipping_done"])
                                         else "already_drawn", "uid": uid, "record": r})

                test_user = is_test_uid(con, uid)

                # ---- 库存与并发 ----
                # 整段都在 BEGIN IMMEDIATE + WRITE_LOCK 里：读「已发出数量」和写 record 之间
                # 不可能被别的请求插进来，所以「某奖品库存 300、同时来 600 个请求」也不会超发。
                # 测试ID走 is_test=True：不受库存约束，正式奖品抽光了也能继续中奖。
                sectors = build_sectors(con, is_test=test_user)

                # ---- 中奖判定：动态概率（与扇区大小无关）----
                # 先按「剩余礼品 ÷ 剩余人数」掷骰子决定中不中，再挑指针停哪个扇区。
                # 整段在 BEGIN IMMEDIATE + WRITE_LOCK 里，「读剩余量 → 写记录」之间
                # 不可能被别的请求插进来，所以并发下也不会超发。
                p_win = dynamic_win_rate(con, is_test=test_user)
                sec = None
                if p_win > 0 and random.random() < p_win:
                    gift = pick_prize(con, is_test=test_user)
                    if gift:
                        sec = next((s for s in sectors
                                    if s.get("is_win") and s.get("prize_id") == gift["id"]), None)
                if sec is None:
                    # 没中奖：随机停在某个「谢谢参与」扇区上
                    blanks = [s for s in sectors if not s.get("is_win")]
                    sec = random.choice(blanks) if blanks else dict(NO_WIN_SECTOR)
                idx = sectors.index(sec) if sec in sectors else 0

                prize_name = sec["name"] if sec["is_win"] else NO_WIN_NAME
                prize_code = (sec.get("code") or "") if sec["is_win"] else ""
                try:
                    con.execute(
                        "INSERT INTO records(uid, prize_id, prize_name, prize_code, is_win, "
                        "is_test, draw_at) VALUES (?,?,?,?,?,?,?)",
                        (uid, (sec.get("prize_id") if sec["is_win"] else None),
                         prize_name, prize_code,
                         1 if sec["is_win"] else 0,
                         1 if test_user else 0,
                         now_str()),
                    )
                except sqlite3.IntegrityError:
                    con.rollback()
                    rec = get_record(con, uid)
                    return self.json_ok({"status": "already_drawn", "uid": uid, "record": record_to_dict(rec)})
                con.commit()
                rec = get_record(con, uid)
                return self.json_ok({
                    "status": "ok", "uid": uid, "sectors": public_sectors(sectors),
                    "sector_index": idx,
                    "is_win": 1 if sec["is_win"] else 0,
                    "prize_name": prize_name, "prize_code": prize_code,
                    "is_test": 1 if test_user else 0,
                    "record": record_to_dict(rec),
                })
            finally:
                put_db(con)

    def api_user_shipping(self, body):
        uid = normalize_uid(body.get("uid"))
        name = (body.get("name") or "").strip()
        phone = (body.get("phone") or "").strip()
        address = (body.get("address") or "").strip()
        province = (body.get("province") or "").strip()[:30]
        city = (body.get("city") or "").strip()[:30]
        district = (body.get("district") or "").strip()[:30]
        if not uid:
            return self.json_err("缺少用户ID")
        if not province or not city or not district:
            return self.json_err("请选择完整的省 / 市 / 区县")
        if not (2 <= len(name) <= 30):
            return self.json_err("请填写正确的收货人姓名（2-30字）")
        digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        if not (7 <= len(digits) <= 20):
            return self.json_err("请填写正确的联系电话")
        if not (5 <= len(address) <= 200):
            return self.json_err("请填写详细的收货地址（5-200字）")
        with WRITE_LOCK:
            con = get_db()
            try:
                con.execute("BEGIN IMMEDIATE")
                rec = get_record(con, uid)
                if not rec:
                    con.rollback()
                    return self.json_err("尚未抽奖，无法提交收货信息")
                if not rec["is_win"]:
                    con.rollback()
                    return self.json_err("未中奖，无需填写收货信息")
                if rec["shipping_at"]:
                    con.rollback()
                    return self.json_ok({"status": "done", "record": record_to_dict(rec),
                                         "message": "收货信息已提交，请勿重复提交"})
                con.execute("UPDATE records SET name=?, phone=?, address=?, province=?, city=?, "
                            "district=?, shipping_at=? WHERE uid=?",
                            (name, digits, address, province, city, district, now_str(), uid))
                con.commit()
                return self.json_ok({"status": "ok", "record": record_to_dict(get_record(con, uid))})
            finally:
                put_db(con)

    # ---------- 管理端：登录 ----------
    def api_admin_login(self, body):
        cfg = load_config()
        pw = body.get("password") or ""
        if not pw:
            return self.json_err("请输入管理密码")
        if not hmac.compare_digest(_hash_pw(pw, cfg["salt"]), cfg["password_hash"]):
            time.sleep(0.4)
            return self.json_err("密码错误", 401)
        token = make_token(cfg)
        return self._send(200, json.dumps({"ok": True}, ensure_ascii=False),
                          "application/json; charset=utf-8",
                          {"Set-Cookie": "admin_token=%s; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax" % token})

    # ---------- 管理端：GET ----------
    def admin_get(self, path, qs):
        con = get_db()
        try:
            if path == "/api/admin/overview":
                return self.json_ok({"stats": stats(con), "server_time": now_str()})

            if path == "/api/admin/users":
                kw = (qs.get("keyword", [""])[0] or "").strip()
                scope = (qs.get("scope", ["all"])[0] or "all").strip()
                page = max(int(qs.get("page", ["1"])[0] or 1), 1)
                size = min(max(int(qs.get("size", ["50"])[0] or 50), 1), 500)
                where, args = [], []
                if kw:
                    where.append("u.uid LIKE ?")
                    args.append("%" + kw + "%")
                if scope == "real":
                    where.append("COALESCE(u.is_test,0)=0")
                elif scope == "test":
                    where.append("COALESCE(u.is_test,0)=1")
                wsql = ("WHERE " + " AND ".join(where)) if where else ""
                total = con.execute("SELECT COUNT(*) c FROM users u %s" % wsql, args).fetchone()["c"]
                rows = con.execute(
                    "SELECT u.uid, u.added_at, u.is_test, u.tag, r.id AS record_id, r.is_win, r.prize_name, "
                    "r.draw_at, r.shipping_at, r.prize_code "
                    "FROM users u LEFT JOIN records r ON r.uid = u.uid %s "
                    "ORDER BY (r.draw_at IS NULL), r.draw_at DESC, u.rowid DESC "
                    "LIMIT ? OFFSET ?" % wsql,
                    args + [size, (page - 1) * size],
                ).fetchall()
                items = []
                for r in rows:
                    items.append({
                        "uid": r["uid"], "added_at": r["added_at"], "drawn": r["record_id"] is not None,
                        "is_win": int(r["is_win"] or 0), "prize_name": r["prize_name"], "draw_at": r["draw_at"],
                        "shipping_done": bool(r["shipping_at"]), "is_test": int(r["is_test"] or 0),
                        "tag": r["tag"] or "", "prize_code": r["prize_code"] or "",
                    })
                counts = con.execute(
                    "SELECT COALESCE(SUM(CASE WHEN COALESCE(is_test,0)=1 THEN 1 ELSE 0 END),0) t, "
                    "COUNT(*) a FROM users"
                ).fetchone()
                return self.json_ok({"total": total, "page": page, "size": size, "items": items,
                                     "test_total": counts["t"], "all_total": counts["a"],
                                     "whitelist_only": setting(con, "whitelist_only", "1") == "1"})

            if path == "/api/admin/users/export.csv":
                rows = con.execute(
                    "SELECT uid, added_at, is_test, tag FROM users ORDER BY rowid").fetchall()
                buf = io.StringIO()
                buf.write("\ufeff")
                w = csv.writer(buf)
                w.writerow(["用户ID", "导入时间", "类型", "标记"])
                for r in rows:
                    w.writerow([r["uid"], r["added_at"], "测试ID" if r["is_test"] else "正式ID",
                                r["tag"] or ""])
                return self.csv_response(buf.getvalue(), "users.csv")

            if path == "/api/admin/prizes":
                """奖品列表 + 当前动态中奖率。"""
                items = list_prizes(con)
                left_stock = sum(max(0, p["stock"] - p["used"]) for p in items if p["stock"] >= 0)
                users_all = con.execute(
                    "SELECT COUNT(*) c FROM users WHERE COALESCE(is_test,0)=0").fetchone()["c"]
                users_drawn = con.execute(
                    "SELECT COUNT(*) c FROM records WHERE COALESCE(is_test,0)=0").fetchone()["c"]
                return self.json_ok({
                    "items": items,
                    "win_rate": round(dynamic_win_rate(con) * 100.0, 4),
                    "test_rate": round(dynamic_win_rate(con, is_test=True) * 100.0, 4),
                    "left_stock": left_stock,
                    "left_users": left_people_count(con),
                    "total_users": users_all,
                    "drawn_users": users_drawn,
                    "no_code": sum(1 for p in items if not (p["code"] or "").strip()),
                })

            if path == "/api/admin/records":
                kw = (qs.get("keyword", [""])[0] or "").strip()
                st = (qs.get("status", [""])[0] or "").strip()
                scope = (qs.get("scope", ["all"])[0] or "all").strip()
                page = max(int(qs.get("page", ["1"])[0] or 1), 1)
                size = min(max(int(qs.get("size", ["50"])[0] or 50), 1), 500)
                where, args = [], []
                if kw:
                    where.append("(uid LIKE ? OR name LIKE ? OR phone LIKE ? OR prize_name LIKE ?)")
                    args += ["%" + kw + "%"] * 4
                if st == "win":
                    where.append("is_win=1")
                elif st == "lose":
                    where.append("is_win=0")
                elif st == "pending":
                    where.append("is_win=1 AND (shipping_at IS NULL OR shipping_at='')")
                elif st == "shipped":
                    where.append("shipping_at IS NOT NULL AND shipping_at<>''")
                elif st == "nocode":
                    where.append("is_win=1 AND TRIM(COALESCE(prize_code,''))=''")
                if scope == "real":
                    where.append("COALESCE(is_test,0)=0")
                elif scope == "test":
                    where.append("COALESCE(is_test,0)=1")
                wsql = ("WHERE " + " AND ".join(where)) if where else ""
                total = con.execute("SELECT COUNT(*) c FROM records %s" % wsql, args).fetchone()["c"]
                rows = con.execute(
                    "SELECT * FROM records %s ORDER BY id DESC LIMIT ? OFFSET ?" % wsql,
                    args + [size, (page - 1) * size],
                ).fetchall()
                return self.json_ok({"total": total, "page": page, "size": size,
                                     "items": [record_to_dict(r) for r in rows]})

            if path == "/api/admin/records/export.csv":
                scope = (qs.get("scope", ["all"])[0] or "all").strip()
                wsql = "" if scope == "all" else (
                    "WHERE COALESCE(is_test,0)=1" if scope == "test" else "WHERE COALESCE(is_test,0)=0")
                rows = con.execute("SELECT * FROM records %s ORDER BY id" % wsql).fetchall()
                buf = io.StringIO()
                buf.write("\ufeff")
                w = csv.writer(buf)
                w.writerow(["序号", "用户ID", "类型", "是否中奖", "奖品", "物料编号",
                            "抽奖时间", "省", "市", "区/县", "详细地址",
                            "收货人", "联系电话", "填写时间"])
                for i, r in enumerate(rows, 1):
                    code = (r["prize_code"] or "").strip()
                    w.writerow([i, r["uid"], "测试ID" if r["is_test"] else "正式ID",
                                "中奖" if r["is_win"] else "未中奖", r["prize_name"] or "",
                                code, r["draw_at"] or "",
                                r["province"] or "", r["city"] or "", r["district"] or "",
                                r["address"] or "", r["name"] or "", r["phone"] or "",
                                r["shipping_at"] or ""])
                return self.csv_response(buf.getvalue(), "records.csv")

            if path == "/api/admin/export/shipping.csv":
                return self.export_shipping(con, qs)

            if path == "/api/admin/settings":
                return self.json_ok({"title": setting(con, "title", DEFAULT_TITLE),
                                     "whitelist_only": setting(con, "whitelist_only", "1") == "1",
                                     "dept_code": setting(con, "dept_code", DEPT_CODE),
                                     "order_title": setting(con, "order_title", ORDER_TITLE),
                                     "order_no": setting(con, "order_no", ORDER_NO),
                                     "region_provinces": REGION_PROVINCE_COUNT,
                                     "version": APP_VERSION,
                                     "db_path": DB_PATH})
            return self.json_err("接口不存在", 404)
        finally:
            put_db(con)

    def export_shipping(self, con, qs):
        """
        发货表：11 列，按下游系统要求固定表头。
        A学生ID* / B部门编号* / C标题 / D业务订单编号 / E物料信息*(编号*1)
        / F省 / G市 / H区县 / I详细地址 / J收货人电话 / K收货人姓名
        scope=filled（默认，只导已填收货信息的）| win（所有中奖记录，含未填）
        include_test=0（默认）：**默认排除测试ID**；=1 才带上测试记录。
        E 列取「中奖的那个奖品」自己的物料编号（记录里存了快照；编号后来才补填的也能回查）。
        """
        scope = (qs.get("scope", ["filled"])[0] or "filled").strip()
        include_test = (qs.get("include_test", ["0"])[0] or "0").strip() == "1"
        dept = setting(con, "dept_code", DEPT_CODE)
        otitle = setting(con, "order_title", ORDER_TITLE)
        ono = setting(con, "order_no", ORDER_NO)
        where = ["r.is_win=1"]
        if scope != "win":
            where.append("r.shipping_at IS NOT NULL AND r.shipping_at<>''")
        if not include_test:
            where.append("COALESCE(r.is_test,0)=0")
        rows = con.execute(
            # 编号优先取记录里的快照；快照为空时回查 prizes（编号后来才补填的情况）
            "SELECT r.*, COALESCE(NULLIF(TRIM(r.prize_code),''), TRIM(COALESCE(p.code,'')), '') "
            "AS code_use FROM records r LEFT JOIN prizes p ON p.id = r.prize_id "
            "WHERE %s ORDER BY r.id" % " AND ".join(where)
        ).fetchall()
        buf = io.StringIO()
        buf.write("\ufeff")   # BOM：Excel 直接双击打开不乱码
        w = csv.writer(buf)
        w.writerow(["学生ID*", "部门编号*", "标题", "业务订单编号", "物料信息*",
                    "省", "市", "区/县", "详细地址", "收货人电话", "收货人姓名"])
        no_code = []
        for r in rows:
            # E 列 = 中奖那个奖品自己的物料编号
            code = (r["code_use"] or "").strip()
            if not code:
                no_code.append(r["uid"])
            w.writerow([
                r["uid"], dept, otitle, ono,
                (code + "*1") if code else "",
                r["province"] or "", r["city"] or "", r["district"] or "",
                r["address"] or "", r["phone"] or "", r["name"] or "",
            ])
        name = "shipping_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.csv_response(buf.getvalue(), name, extra_note=no_code)

    def csv_response(self, text, filename, extra_note=None):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 有奖品没填物料编号时，把受影响的 ID 回给前端提示（换行分隔，非 ASCII 不能进 HTTP 头）
        if extra_note:
            self.send_header("X-No-Code-Count", str(len(extra_note)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 管理端：POST ----------
    def admin_post(self, path, body):
        if path == "/api/admin/users":
            text = body.get("text") or ""
            mode = body.get("mode") or "append"
            is_test = bool(body.get("is_test"))
            tag = str(body.get("tag") or "").strip()[:40]
            ids = parse_id_list(text)
            if not ids and mode != "replace":
                return self.json_err("没有解析到有效的用户ID")
            with WRITE_LOCK:
                con = get_db()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    if mode == "replace":
                        # 只清同类型的名单，避免「导入测试ID」把正式名单一起冲掉
                        if is_test:
                            con.execute("DELETE FROM users WHERE COALESCE(is_test,0)=1")
                        else:
                            con.execute("DELETE FROM users WHERE COALESCE(is_test,0)=0")
                    added = skipped = 0
                    for uid in ids:
                        cur = con.execute(
                            "INSERT OR IGNORE INTO users(uid, added_at, is_test, tag) VALUES (?,?,?,?)",
                            (uid, now_str(), 1 if is_test else 0, tag or ("test" if is_test else "admin")))
                        if cur.rowcount:
                            added += 1
                        else:
                            # 已存在：显式把标记刷成这次导入的类型（便于把误加的正式ID改成测试ID）
                            con.execute("UPDATE users SET is_test=?, tag=? WHERE uid=?",
                                        (1 if is_test else 0, tag or ("test" if is_test else "admin"), uid))
                            skipped += 1
                    con.commit()
                    row = con.execute(
                        "SELECT COUNT(*) a, COALESCE(SUM(CASE WHEN COALESCE(is_test,0)=1 THEN 1 ELSE 0 END),0) t "
                        "FROM users").fetchone()
                    return self.json_ok({"added": added, "skipped": skipped,
                                         "total": row["a"], "test_total": row["t"],
                                         "is_test": 1 if is_test else 0})
                finally:
                    put_db(con)

        if path == "/api/admin/users/flag":
            """把已有用户标记/取消标记为测试ID。"""
            ids = parse_id_list(body.get("uids") or "")
            is_test = 1 if body.get("is_test") else 0
            if not ids:
                return self.json_err("没有解析到有效的用户ID")
            with WRITE_LOCK:
                con = get_db()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    changed = 0
                    for uid in ids:
                        cur = con.execute("UPDATE users SET is_test=? WHERE uid=?", (is_test, uid))
                        changed += cur.rowcount or 0
                        # 已抽过的记录也一起改，否则旧记录还会占库存/进发货表
                        con.execute("UPDATE records SET is_test=? WHERE uid=?", (is_test, uid))
                    con.commit()
                    row = con.execute(
                        "SELECT COUNT(*) a, COALESCE(SUM(CASE WHEN COALESCE(is_test,0)=1 THEN 1 ELSE 0 END),0) t "
                        "FROM users").fetchone()
                    return self.json_ok({"changed": changed, "total": row["a"], "test_total": row["t"]})
                finally:
                    put_db(con)

        if path == "/api/admin/prizes":
            """保存奖品列表：每个奖品各自一份 名称 / 物料编号 / 概率 / 库存。

            整表覆盖式保存 —— 前端传什么就是什么，没传的会被删掉。
            允许传空列表（= 清空全部奖品），此时转盘上只剩「谢谢参与」。
            """
            items = body.get("items")
            if not isinstance(items, list):
                return self.json_err("奖品列表格式不对")
            if len(items) > 60:
                return self.json_err("奖品最多 60 个")
            clean = []
            for i, it in enumerate(items):
                name = (it.get("name") or "").strip()
                if not name:
                    return self.json_err("第 %d 个奖品的名称为空" % (i + 1))
                code = (it.get("code") or "").strip().replace("*", "")[:32]
                try:
                    prob = float(it.get("prob") or 0)
                except (TypeError, ValueError):
                    return self.json_err("第 %d 个奖品的概率格式不对" % (i + 1))
                if prob < 0:
                    return self.json_err("第 %d 个奖品的概率不能是负数" % (i + 1))
                if prob > 100:
                    prob = 100.0
                stock = to_int(it.get("stock"), -1)
                if stock < -1:
                    stock = -1
                clean.append((name[:40], code, prob, stock, i))
            with WRITE_LOCK:
                con = get_db()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    keep_ids = [int(it["id"]) for it in items if it.get("id")]
                    if keep_ids:
                        q = ",".join("?" * len(keep_ids))
                        con.execute("DELETE FROM prizes WHERE id NOT IN (%s)" % q, keep_ids)
                    else:
                        con.execute("DELETE FROM prizes")
                    for name, code, prob, stock, i in clean:
                        pid = items[i].get("id")
                        if pid:
                            con.execute("UPDATE prizes SET name=?, code=?, prob=?, stock=?, sort=? "
                                        "WHERE id=?", (name, code, prob, stock, i, int(pid)))
                        else:
                            con.execute("INSERT INTO prizes(name, code, prob, stock, sort) "
                                        "VALUES (?,?,?,?,?)", (name, code, prob, stock, i))
                    # 编号是后来才补填的：把老记录的编号快照一并补齐（只补空的）
                    con.execute(
                        "UPDATE records SET prize_code = COALESCE("
                        "  (SELECT TRIM(code) FROM prizes WHERE prizes.id = records.prize_id), '') "
                        "WHERE is_win=1 AND TRIM(COALESCE(prize_code,''))=''"
                    )
                    con.commit()
                    return self.json_ok({"saved": len(clean),
                                         "win_rate": round(dynamic_win_rate(con) * 100.0, 4),
                                         "no_code": sum(1 for c in clean if not c[1])})
                finally:
                    put_db(con)

        if path == "/api/admin/settings":
            with WRITE_LOCK:
                con = get_db()
                try:
                    con.execute("BEGIN IMMEDIATE")
                    if "whitelist_only" in body:
                        set_setting(con, "whitelist_only", "1" if body.get("whitelist_only") else "0")
                    if body.get("title"):
                        set_setting(con, "title", str(body["title"]).strip()[:40])
                    for key, val in (("dept_code", "dept_code"),
                                     ("order_title", "order_title"),
                                     ("order_no", "order_no")):
                        v = body.get(key)
                        if v is not None:
                            set_setting(con, key, str(v).strip()[:60])
                    con.commit()
                    return self.json_ok({"title": setting(con, "title", DEFAULT_TITLE),
                                         "whitelist_only": setting(con, "whitelist_only", "1") == "1",
                                         "dept_code": setting(con, "dept_code", DEPT_CODE),
                                         "order_title": setting(con, "order_title", ORDER_TITLE),
                                         "order_no": setting(con, "order_no", ORDER_NO)})
                finally:
                    put_db(con)

        if path == "/api/admin/password":
            old = body.get("old") or ""
            new = body.get("new") or ""
            if len(new) < 6:
                return self.json_err("新密码至少 6 位")
            cfg = load_config()
            if not hmac.compare_digest(_hash_pw(old, cfg["salt"]), cfg["password_hash"]):
                return self.json_err("原密码不正确", 401)
            salt = secrets.token_hex(16)
            cfg["salt"] = salt
            cfg["password_hash"] = _hash_pw(new, salt)
            save_config(cfg)
            return self._send(200, json.dumps({"ok": True, "message": "密码已修改，请重新登录"},
                                              ensure_ascii=False), "application/json; charset=utf-8",
                              {"Set-Cookie": "admin_token=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        return self.json_err("接口不存在", 404)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
# 并发承载说明（600 峰值做过实测，见 README「并发承载」一节）：
#   · ThreadingHTTPServer 是「一个连接一个线程」。Windows 上进程线程数到 ~260 就顶不住了，
#     再多的连接会被内核 RST，客户端报「远程主机强迫关闭了一个现有的连接」。
#     服务器上跑的是 Linux（Docker），线程/句柄上限高得多，600 并发没有这个问题。
#   · 为了两条路都稳，这里不用「来多少起多少线程」的默认行为，改成
#     **固定大小的工作线程池 + 连接排队**：超出的连接先 accept 进来放进队列等着，
#     而不是把 listen 队列塞爆后被内核拒绝。
#   · backlog 也从默认的 5 拉到 1024，让突发连接有地方排队。
LISTEN_BACKLOG = int(os.environ.get("LISTEN_BACKLOG", "1024"))
# 工作线程数：够撑住 600 峰值（抽奖是纯 SQLite 短事务，单次几毫秒）。
# 想要更保守可以环境变量 WORKER_THREADS=200 调小。
WORKER_THREADS = int(os.environ.get("WORKER_THREADS", "256"))
# 连接排队上限：满了才拒绝，正常情况用不到。
MAX_QUEUED = int(os.environ.get("MAX_QUEUED", "4096"))


class PooledHTTPServer(ThreadingHTTPServer):
    """固定线程池 + 连接排队的 HTTP 服务器。

    相比默认的 ThreadingHTTPServer（每个请求新建一个线程），这里的好处是：
      1) 线程数有上限，不会因为突发流量把进程线程数打爆；
      2) 超出的连接被 accept 后进队列等待，而不是被内核重置 —— 用户看到的是「稍慢」而不是「抽不了」；
      3) 单个连接出错不刷屏。

    实现要点：覆盖 `process_request`（socketserver 的线程入口），
    而不是 `process_request_thread`（那是工作线程体，覆盖它会自己把自己递归回去）。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = LISTEN_BACKLOG
    # 不参与 ThreadingMixIn 的线程登记（我们自己管线程），否则 server_close 会 join 一堆空线程
    block_on_close = False

    def __init__(self, addr, handler):
        self._queue = queue.Queue(maxsize=MAX_QUEUED)
        self._workers = []
        self._stopped = False
        super().__init__(addr, handler)
        self._start_workers()

    def _start_workers(self):
        for _ in range(WORKER_THREADS):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()
            self._workers.append(t)

    def _worker(self):
        while not self._stopped:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            request, client_address = item
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
                self._queue.task_done()

    def process_request(self, request, client_address):
        """把连接塞进工作队列，而不是每连接起一个新线程。"""
        try:
            self._queue.put_nowait((request, client_address))
        except queue.Full:
            # 队列也满了：在当前（accept 主循环）线程里同步处理，形成天然背压。
            # 宁可慢一点，也不丢请求。
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)

    def handle_error(self, request, client_address):
        """单个连接出错不刷屏（高频并发下客户端中途断开很常见），只记一行。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError, OSError)):
            return
        sys.stderr.write("[%s] 处理 %s 出错：%s\n" % (now_str(), client_address, exc))

    def server_close(self):
        self._stopped = True
        # 排空队列（未处理的连接直接关掉，避免进程退不出去）
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                try:
                    self.shutdown_request(item[0])
                except Exception:
                    pass
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
        super().server_close()


def main():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
        if a in ("--host",) and i + 1 < len(argv):
            host = argv[i + 1]

    fresh = not os.path.exists(CONFIG_PATH)
    init_db()
    _prewarm_pool()
    load_config()

    local_ip = "127.0.0.1"
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("=" * 62)
    print("  幸运大转盘 已启动   v%s" % APP_VERSION)
    print("-" * 62)
    print("  用户端：http://127.0.0.1:%d/" % port)
    print("  管理端：http://127.0.0.1:%d/admin" % port)
    if host == "0.0.0.0":
        print("  局域网：http://%s:%d/   （同事手机/电脑可直接访问）" % (local_ip, port))
    print("  数据文件：%s" % DB_PATH)
    print("  行政区划：%s%s" % (REGIONS_PATH,
          "" if REGION_PROVINCE_COUNT else "   ← 缺失！用户端无法选择省市区"))
    if fresh:
        print("-" * 62)
        print("  首次运行，管理端默认密码：%s   ← 登录后请立即修改" % DEFAULT_PASSWORD)
    print("=" * 62)
    print("  按 Ctrl+C 停止服务\n")
    sys.stdout.flush()   # 输出重定向到文件时也能立刻看到地址

    srv = PooledHTTPServer((host, port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
