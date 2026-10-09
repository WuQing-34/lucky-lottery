# -*- coding: utf-8 -*-
"""v3.1 动态中奖率验证：扇区等分 + 中奖率 = 剩余礼品 ÷ 剩余人数。

覆盖：
  1. 扇区等分（5 奖品 + 5 谢谢参与 = 10 块，各 36°），奖品与谢谢参与交替排列
  2. 动态中奖率公式 = 剩余库存 ÷ 剩余人数（200 人 / 10 件 → 5.00%）
  3. 跑完 200 人：礼品基本发完、绝不超发、各奖品发得均衡
  4. 人都抽完 → 中奖率归 0
  5. 库存发完 → 中奖率归 0
  6. 测试ID 不受库存限制（正式发完了它照样能中）
  7. 全部不限量（-1）→ 退回固定 10%，不会变成 0

用法：python test_v4_dynamic_rate.py
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
PORT = 8797
BASE = "http://127.0.0.1:%d" % PORT
TMP = tempfile.mkdtemp(prefix="lottery_v31_")
DB = os.path.join(TMP, "lottery.db")
FAILED, PASSED = [], 0
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ok(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s   %s" % (name, extra))


def call(path, body=None, cookie=None):
    headers, data = {}, None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)


def fetch(path, cookie):
    """取原始字节（导出 CSV 用）。"""
    req = urllib.request.Request(BASE + path, headers={"Cookie": cookie})
    with _OPENER.open(req, timeout=20) as r:
        return r.read()


def sql(q, args=()):
    c = sqlite3.connect(DB, timeout=10)
    try:
        rows = c.execute(q, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def start():
    env = dict(os.environ)
    for k in list(env):
        if k.upper().endswith("_PROXY"):
            env.pop(k, None)
    env.update({"LOTTERY_DATA_DIR": TMP, "PORT": str(PORT), "HOST": "127.0.0.1",
                "DB_POOL_PREWARM": "2"})
    log = open(os.path.join(TMP, "srv.log"), "wb")
    p = subprocess.Popen([sys.executable, APP], env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(120):
        try:
            with _OPENER.open(BASE + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return p
        except Exception:
            time.sleep(0.15)
    log.close()
    sys.stdout.write(open(os.path.join(TMP, "srv.log"), encoding="utf-8", errors="replace").read()[-2000:])
    raise SystemExit("服务启动失败")


def main():
    p = start()
    try:
        st, d, h = call("/api/admin/login", {"password": "admin888"})
        ck = [v for k, v in h.items() if k.lower() == "set-cookie"][0].split(";")[0]

        print("\n--- 1. 扇区等分 + 谢谢参与穿插 ---")
        names = ["积木", "盲盒笔", "毛毡本", "发声书", "收纳箱"]
        st, d, _ = call("/api/admin/prizes",
                        {"items": [{"name": n, "code": "C%d" % i, "prob": 0, "stock": 2}
                                   for i, n in enumerate(names)]}, cookie=ck)
        ok("保存 5 个奖品", d.get("saved") == 5, d)

        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        ok("扇区数 = 10（5 奖品 + 5 谢谢参与）", len(secs) == 10, len(secs))
        ok("全部等分 36°", all(abs(s["angle"] - 36.0) < 1e-6 for s in secs),
           sorted({round(s["angle"], 3) for s in secs}))
        ok("角度累加 360°", abs(sum(s["angle"] for s in secs) - 360) < 1e-6,
           sum(s["angle"] for s in secs))
        ok("奖品与谢谢参与交替", [s["is_win"] for s in secs] == [1, 0] * 5,
           [s["is_win"] for s in secs])
        ok("前端看不到库存字段",
           all(("stock" not in s and "used" not in s and "left" not in s and "live" not in s)
               for s in secs), secs[0])

        print("\n--- 2. 动态中奖率 = 剩余礼品 ÷ 剩余人数 ---")
        N = 200
        call("/api/admin/users", {"mode": "replace",
                                  "text": "\n".join("U%03d" % i for i in range(N))}, cookie=ck)
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("200 人 / 10 件 → 中奖率 5.00%", abs(d["win_rate"] - 5.0) < 0.01, d["win_rate"])
        ok("剩余礼品 = 10", d["left_stock"] == 10, d["left_stock"])
        ok("尚未参与 = 200", d["left_users"] == 200, d["left_users"])

        print("\n--- 3. 跑完 200 人：发得完、不超发、够均衡 ---")
        wins = 0
        for i in range(N):
            st, r, _ = call("/api/user/spin", {"uid": "U%03d" % i})
            if r.get("is_win") == 1:
                wins += 1
        ok("中奖数不超过库存 10（不超发）", wins <= 10, wins)
        ok("礼品基本发完（≥8）", wins >= 8, wins)
        print("      实际中出 %d / 10 件" % wins)

        st, d, _ = call("/api/admin/prizes", cookie=ck)
        used = [x["used"] for x in d["items"]]
        ok("各奖品发出数合计 = 中奖数", sum(used) == wins, (used, wins))
        ok("各奖品发得均衡（极差≤2）", max(used) - min(used) <= 2, used)
        ok("剩余礼品 = 10 - 中奖数", d["left_stock"] == 10 - wins, (d["left_stock"], wins))
        ok("剩余人数归 0", d["left_users"] == 0, d["left_users"])
        ok("人都抽完了 → 中奖率归 0", abs(d["win_rate"]) < 1e-9, d["win_rate"])

        print("\n--- 4. 库存发完 → 中奖率归 0 ---")
        call("/api/admin/users", {"mode": "replace",
                                  "text": "A1\nA2\nA3\nA4\nA5"}, cookie=ck)
        st, d, _ = call("/api/admin/prizes",
                        {"items": [{"name": "孤品", "code": "X1", "prob": 0, "stock": 1}]}, cookie=ck)
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("5 人 / 1 件 → 中奖率 20.00%", abs(d["win_rate"] - 20.0) < 0.01, d["win_rate"])

        # 直接构造「刚好发完」：插一条该奖品的中奖记录，避开随机性
        sql("INSERT INTO records(uid, prize_id, prize_name, prize_code, is_win, is_test, draw_at) "
            "SELECT 'ZZZ', id, name, code, 1, 0, '2026-10-09 00:00:00' FROM prizes WHERE name='孤品'")
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("已发完 → 剩余 0 件", d["left_stock"] == 0, d["left_stock"])
        ok("已发完 → 中奖率归 0", abs(d["win_rate"]) < 1e-9, d["win_rate"])

        st, d, _ = call("/api/user/prizes")
        ok("发完的奖品仍留在转盘上（不下架）",
           any(s["name"] == "孤品" for s in d["sectors"]), [s["name"] for s in d["sectors"]])

        win_names = []
        for uid in ("A1", "A2", "A3", "A4", "A5"):
            st, r, _ = call("/api/user/spin", {"uid": uid})
            if r.get("is_win") == 1:
                win_names.append(r.get("prize_name"))
        ok("发完后 5 个人全抽不到", not win_names, win_names)

        print("\n--- 5. 测试ID 不受库存限制 ---")
        # 只导入 1 个测试号：此时「1 件库存 ÷ 1 个测试号」= 100%，必然中奖，断言才稳定
        call("/api/admin/users", {"mode": "append", "is_test": True, "text": "T01"}, cookie=ck)
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("正式中奖率 0 时，测试ID 的中奖率 > 0", d["test_rate"] > 0, d["test_rate"])
        st, r, _ = call("/api/user/spin", {"uid": "T01"})
        ok("测试ID 在正式库存发完后照样能中",
           r.get("is_win") == 1 and r.get("is_test") == 1, r.get("prize_name"))

        print("\n--- 6. 全部不限量 → 退回固定 10% ---")
        call("/api/admin/users", {"mode": "replace", "text": "B1\nB2"}, cookie=ck)
        st, d, _ = call("/api/admin/prizes",
                        {"items": [{"name": "不限量奖", "code": "Y1", "prob": 0, "stock": -1}]}, cookie=ck)
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("全不限量时退回固定 10%", abs(d["win_rate"] - 10.0) < 1e-6, d["win_rate"])
        ok("不限量奖品的剩余显示为「不限」", d["items"][0]["left"] == "不限", d["items"][0])

        print("\n--- 7. 发货表 E 列 = 中奖奖品编号 ×1 ---")
        call("/api/admin/users", {"mode": "replace", "text": "S001"}, cookie=ck)
        call("/api/admin/prizes",
             {"items": [{"name": "保温杯", "code": "B001", "prob": 0, "stock": 1}]}, cookie=ck)
        st, r, _ = call("/api/user/spin", {"uid": "S001"})
        ok("1 人 / 1 件 → 必然中奖", r.get("is_win") == 1, r.get("prize_name"))
        ok("响应带 prize_code=B001", r.get("prize_code") == "B001", r.get("prize_code"))
        st, r, _ = call("/api/user/shipping", {"uid": "S001", "name": "张三", "phone": "13800001111",
                                              "province": "广东省", "city": "深圳市",
                                              "district": "南山区", "address": "科技园路1号"})
        ok("提交收货信息成功", r.get("ok") is True, r)

        raw = fetch("/api/admin/export/shipping.csv", ck)
        ok("发货表带 UTF-8 BOM", raw[:3] == b"\xef\xbb\xbf", raw[:6])
        lines = [l for l in raw.decode("utf-8").strip().splitlines() if l.strip()]
        ok("发货表表头 11 列", len(lines[0].split(",")) == 11, lines[0])
        body = lines[1] if len(lines) > 1 else ""
        ok("E 列 = B001*1", ",B001*1," in body, body)
        ok("B 列 = 部门编号 1000003263", "1000003263" in body, body)
        ok("C/D 列固定文案", body.count("双节课消活动礼品") == 2, body)
        ok("F/G/H 省市区齐全", ("广东省" in body) and ("深圳市" in body) and ("南山区" in body), body)

        print("\n--- 8. 其他基础检查 ---")
        st, d, _ = call("/api/admin/overview")
        ok("未登录访问看板 = 401", st == 401, st)
        st, d, _ = call("/api/regions")
        ok("省市区数据可拉取（34 省）", st == 200 and isinstance(d, list) and len(d) == 34, st)
        raw = fetch("/api/admin/records/export.csv", ck)
        ok("明细 CSV 带 BOM 且 14 列",
           raw[:3] == b"\xef\xbb\xbf"
           and len(raw.decode("utf-8").strip().splitlines()[0].split(",")) == 14, raw[:6])
        st, d, _ = call("/api/user/spin", {"uid": "S001"})
        ok("同一ID 不能抽第二次", d.get("status") in ("already_drawn", "need_shipping"), d.get("status"))

        print("\n--- 9. 空奖品 ---")
        call("/api/admin/prizes", {"items": []}, cookie=ck)
        st, d, _ = call("/api/user/prizes")
        ok("没有奖品时转盘只剩一块谢谢参与",
           len(d["sectors"]) == 1 and d["sectors"][0]["name"] == "谢谢参与", d["sectors"])
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("没有奖品时中奖率 0", abs(d["win_rate"]) < 1e-9, d["win_rate"])

    finally:
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    print("\n" + "=" * 58)
    if FAILED:
        print("  通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
        for f in FAILED:
            print("    × %s" % f)
        print("  服务日志：%s" % TMP)
        print("=" * 58)
        return 1
    print("  通过 %d 项，失败 0 项" % PASSED)
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
