# -*- coding: utf-8 -*-
"""v1/v2 老库 → v3 升级路径验证。

上次改版就是在「老库自动迁移」这条路上翻过车（建索引跑在补列之前 →
容器崩溃循环），所以这里专门盯死它：

  1. 造一个**缺 gift_* 列**的老库（v1 形态，schema_version 缺失）
  2. 用新版直接启动
  3. 断言：
     · 服务能起来（不崩）
     · 缺的列被补上
     · 奖品表被清空（v3 迁移动作）
     · **抽奖记录 / 用户名单 / 设置项全部保留**
     · schema_version 升到 3
     · 二次重启走快速路径，数据不被再次改动

用法：python test_v3_migration.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # lottery/
APP = os.path.join(ROOT, "app.py")
PORT = 8796
BASE = "http://127.0.0.1:%d" % PORT

TMP = tempfile.mkdtemp(prefix="lottery_mig_")
DB = os.path.join(TMP, "lottery.db")
FAILED = []
PASSED = 0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ok(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s   %s" % (name, extra))


def q(sql, args=()):
    c = sqlite3.connect(DB, timeout=10)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def build_old_db():
    """造一个 v1 形态的老库：records 表没有 gift_id/gift_name/gift_code/is_test。"""
    if os.path.exists(DB):
        os.remove(DB)
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE users (
        uid TEXT PRIMARY KEY, added_at TEXT
    );
    CREATE TABLE prizes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        code TEXT NOT NULL DEFAULT '', prob REAL NOT NULL DEFAULT 0,
        stock INTEGER NOT NULL DEFAULT -1, sort INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE records (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE,
        prize_id INTEGER, prize_name TEXT, is_win INTEGER NOT NULL DEFAULT 0,
        draw_at TEXT, name TEXT, phone TEXT, address TEXT, shipping_at TEXT
    );
    CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT);

    INSERT INTO prizes(name, code, prob, stock, sort) VALUES ('限量测试品','T1',0,-1,0);
    INSERT INTO prizes(name, code, prob, stock, sort) VALUES ('礼品A','',0,-1,1);
    INSERT INTO users(uid, added_at) VALUES ('OLD1','2026-10-08 09:00:00');
    INSERT INTO users(uid, added_at) VALUES ('OLD2','2026-10-08 09:01:00');
    INSERT INTO records(uid, prize_id, prize_name, is_win, draw_at)
        VALUES ('OLD1', 1, '限量测试品', 1, '2026-10-08 10:00:00');
    INSERT INTO settings VALUES('whitelist_only','1');
    INSERT INTO settings VALUES('title','幸运大转盘');
    INSERT INTO settings VALUES('pool_prob','10');
    INSERT INTO settings VALUES('round_total_stock','5');
    INSERT INTO settings VALUES('prize_pool_name','实物周边');
    """)
    c.commit()
    c.close()


def start(wait=1):
    env = dict(os.environ)
    for k in list(env):
        if k.upper().endswith("_PROXY"):
            env.pop(k, None)
    env["LOTTERY_DATA_DIR"] = TMP
    env["PORT"] = str(PORT)
    env["HOST"] = "127.0.0.1"
    env["DB_POOL_PREWARM"] = "2"
    log = open(os.path.join(TMP, "srv.log"), "ab")
    p = subprocess.Popen([sys.executable, APP], env=env, stdout=log, stderr=subprocess.STDOUT)
    if wait:
        for _ in range(120):
            try:
                with _OPENER.open(BASE + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        return p
            except Exception:
                time.sleep(0.15)
        log.close()
        sys.stdout.write(open(os.path.join(TMP, "srv.log"), encoding="utf-8", errors="replace").read()[-3000:])
        raise SystemExit("服务未启动")
    return p


def stop(p):
    try:
        p.terminate()
        p.wait(timeout=8)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def cols(table):
    return {r[1] for r in q("PRAGMA table_info(%s)" % table)}


def main():
    build_old_db()
    print("老库已就绪：records 列 = %s" % sorted(cols("records")))
    ok("老库确实没有 gift_id 列", "gift_id" not in cols("records"))
    ok("老库确实没有 is_test 列", "is_test" not in cols("records"))

    print("\n--- 第一次启动（触发迁移）---")
    p = start()
    try:
        st = _OPENER.open(BASE + "/api/health", timeout=5).status
        ok("服务正常启动（没崩）", st == 200, st)

        print("\n--- 迁移结果 ---")
        rc = cols("records")
        ok("已补上 gift_id", "gift_id" in rc, sorted(rc))
        ok("已补上 gift_name", "gift_name" in rc)
        ok("已补上 gift_code", "gift_code" in rc)
        ok("已补上 is_test", "is_test" in rc)
        ok("已补上 prize_code", "prize_code" in rc)
        ok("已补上 province/city/district", {"province", "city", "district"} <= rc)
        ok("users 表已补 is_test/tag", {"is_test", "tag"} <= cols("users"))

        pr = q("SELECT COUNT(*) FROM prizes")[0][0]
        ok("奖品表被清空（v3 迁移动作）", pr == 0, pr)

        rec = q("SELECT COUNT(*) FROM records")[0][0]
        ok("抽奖记录保留（1 条）", rec == 1, rec)
        usr = q("SELECT COUNT(*) FROM users")[0][0]
        ok("用户名单保留（2 条）", usr == 2, usr)

        sv = dict(q("SELECT k, v FROM settings"))
        ok("白名单设置保留", sv.get("whitelist_only") == "1", sv.get("whitelist_only"))
        ok("活动标题保留", sv.get("title") == "幸运大转盘", sv.get("title"))
        ok("schema_version 升到 3", sv.get("schema_version") == "3", sv.get("schema_version"))

        # 索引也得建出来（上次就是这一步先于补列执行导致崩溃）
        idx = {r[1] for r in q("SELECT * FROM sqlite_master WHERE type='index'")}
        ok("三个索引都在", {"idx_records_draw", "idx_records_prize", "idx_records_gift"} <= idx, sorted(idx))

        print("\n--- 升级后的功能自检 ---")
        st, d, _ = _get("/api/user/prizes")
        ok("转盘只剩谢谢参与（奖品已清空）", [x["name"] for x in d["sectors"]] == ["谢谢参与"], d["sectors"])
        st, d, _ = _get("/api/user/login", {"uid": "OLD1"})
        ok("老记录还能查到（已抽过）", d.get("status") in ("already_drawn", "need_shipping"), d.get("status"))

    finally:
        stop(p)

    print("\n--- 第二次启动（应走快速路径，不做任何改动）---")
    before = dict(q("SELECT k, v FROM settings"))
    p = start()
    try:
        st = _OPENER.open(BASE + "/api/health", timeout=5).status
        ok("二次启动正常", st == 200, st)
        ok("奖品表仍为空（没被重灌默认值）", q("SELECT COUNT(*) FROM prizes")[0][0] == 0)
        ok("记录数没变", q("SELECT COUNT(*) FROM records")[0][0] == 1)
        after = dict(q("SELECT k, v FROM settings"))
        ok("设置项没被改动", before == after, (before.get("schema_version"), after.get("schema_version")))
    finally:
        stop(p)

    print("\n" + "=" * 58)
    if FAILED:
        print("  通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
        for f in FAILED:
            print("    × %s" % f)
        print("  临时目录：%s" % TMP)
        print("=" * 58)
        return 1
    print("  通过 %d 项，失败 0 项" % PASSED)
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


def _get(path, body=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)


if __name__ == "__main__":
    sys.exit(main())
