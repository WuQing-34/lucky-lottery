# -*- coding: utf-8 -*-
"""v3.0 验收测试：多奖品独立抽奖。

覆盖点：
  1. 空奖品时转盘只有「谢谢参与」
  2. 每个奖品一个扇区；角度累加 360；前端响应里不含库存内部字段
  3. 单奖品库存耗尽 → 扇区保留、概率归零、再也抽不到，且前端看不到「已抢完」
  4. 测试ID 不受库存限制，正式库存抽光后仍能中奖
  5. 概率之和 >100 时按比例压缩到 100（必中、无谢谢参与）
  6. 概率全 0 时整盘谢谢参与
  7. 发货表 E 列 = 中奖奖品的物料编号 ×1，11 列表头，带 BOM
  8. 同一ID 只能抽一次
  9. 允许把奖品表清空（保存空列表）

用法：python test_v3_multi_prize.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # lottery/
APP = os.path.join(ROOT, "app.py")
PORT = 8795
BASE = "http://127.0.0.1:%d" % PORT

TMP = tempfile.mkdtemp(prefix="lottery_v3_")
FAILED = []
PASSED = 0

# 沙箱里的 HTTP_PROXY 会限制单机连接数，不绕开会挂死
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ok(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s   %s" % (name, extra))


def call(path, body=None, cookie=None, raw=False):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=20) as r:
            b = r.read()
            return (r.status, b, dict(r.headers)) if raw else (r.status, json.loads(b.decode("utf-8")), dict(r.headers))
    except urllib.error.HTTPError as e:
        b = e.read()
        if raw:
            return e.code, b, dict(e.headers)
        try:
            return e.code, json.loads(b.decode("utf-8")), dict(e.headers)
        except Exception:
            return e.code, {"ok": False, "error": b.decode("utf-8", "replace")}, dict(e.headers)


def _get(headers, name):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def start():
    env = dict(os.environ)
    for k in list(env):
        if k.upper().endswith("_PROXY"):
            env.pop(k, None)
    env["LOTTERY_DATA_DIR"] = TMP
    env["PORT"] = str(PORT)
    env["HOST"] = "127.0.0.1"
    env["DB_POOL_PREWARM"] = "2"
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
    sys.stdout.write(open(os.path.join(TMP, "srv.log"), encoding="utf-8", errors="replace").read())
    raise SystemExit("服务启动失败")


def add_users(ck, text, is_test=False, mode="append"):
    return call("/api/admin/users", {"text": text, "is_test": is_test, "mode": mode}, cookie=ck)


def save_prizes(ck, items):
    return call("/api/admin/prizes", {"items": items}, cookie=ck)


def main():
    p = start()
    try:
        st, d, h = call("/api/admin/login", {"password": "admin888"})
        ck = _get(h, "Set-Cookie").split(";")[0]
        if "admin_token=" not in ck:
            raise SystemExit("管理员登录失败：%s" % d)

        print("\n--- 1. 初始态：奖品为空 ---")
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("初始奖品表为空", d.get("items") == [], d)
        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        ok("空奖品时转盘只有谢谢参与", len(secs) == 1 and secs[0]["name"] == "谢谢参与", secs)

        print("\n--- 2. 配 3 个奖品（各自概率/库存/编号）---")
        st, d, _ = save_prizes(ck, [
            {"name": "奖品甲", "code": "P001", "prob": 30, "stock": 2},
            {"name": "奖品乙", "code": "P002", "prob": 20, "stock": -1},
            {"name": "奖品丙", "code": "P003", "prob": 50, "stock": 100},
        ])
        ok("保存 3 个奖品", d.get("saved") == 3, d)
        ok("概率合计=100 → 未中奖 0", abs(d.get("prob_sum", 0) - 100) < 0.01 and abs(d.get("no_win_prob", -1)) < 0.01, d)

        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        names = [s["name"] for s in secs]
        ok("转盘 = 3 个奖品扇区（合计 100% 时无谢谢参与）",
           names == ["奖品甲", "奖品乙", "奖品丙"], names)
        ok("角度累加=360", abs(sum(s["angle"] for s in secs) - 360) < 0.1, [s["angle"] for s in secs])
        ok("前端响应不含库存内部字段", all(("stock" not in s and "used" not in s and "sold_out" not in s) for s in secs), secs)

        # 记录 id 供后续沿用（整表覆盖式保存必须带 id，否则会被当新奖品重建）
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        P = {x["name"]: x for x in d["items"]}
        p1, p2, p3 = P["奖品甲"], P["奖品乙"], P["奖品丙"]

        # 概率合计不足 100 时，剩下的自动成为「谢谢参与」
        save_prizes(ck, [
            {"id": p1["id"], "name": "奖品甲", "code": "P001", "prob": 30, "stock": 2},
            {"id": p2["id"], "name": "奖品乙", "code": "P002", "prob": 20, "stock": -1},
            {"id": p3["id"], "name": "奖品丙", "code": "P003", "prob": 30, "stock": 100},
        ])
        st, d, _ = call("/api/user/prizes")
        secs2 = d["sectors"]
        ok("概率合计 80 → 自动补谢谢参与 20",
           [s["name"] for s in secs2] == ["奖品甲", "奖品乙", "奖品丙", "谢谢参与"]
           and abs(secs2[3]["prob"] - 20) < 1e-6, secs2)
        # 恢复 30/20/50，供后面的库存测试用
        save_prizes(ck, [
            {"id": p1["id"], "name": "奖品甲", "code": "P001", "prob": 30, "stock": 2},
            {"id": p2["id"], "name": "奖品乙", "code": "P002", "prob": 20, "stock": -1},
            {"id": p3["id"], "name": "奖品丙", "code": "P003", "prob": 50, "stock": 100},
        ])

        print("\n--- 3. 抽奖：奖品甲库存只有 2 ---")
        ids = ["U%03d" % i for i in range(1, 21)]
        st, d, _ = add_users(ck, "\n".join(ids), mode="replace")
        ok("导入 20 个正式ID", d.get("added") == 20, d)
        got = []
        for uid in ids:
            st, d, _ = call("/api/user/spin", {"uid": uid})
            got.append(d)
        ok("20 次抽奖都有响应", all(g.get("status") == "ok" for g in got))
        a_cnt = len([g for g in got if g.get("is_win") and g.get("prize_name") == "奖品甲"])
        ok("奖品甲恰好中出 2 次（=库存）", a_cnt == 2, a_cnt)

        st, d, _ = call("/api/admin/prizes", cookie=ck)
        pa = [x for x in d["items"] if x["name"] == "奖品甲"][0]
        ok("奖品甲已发放=2", pa["used"] == 2, pa)
        ok("奖品甲剩余=0", pa["left"] == 0, pa)
        ok("管理端标记 sold_out=1", pa["sold_out"] == 1, pa)

        print("\n--- 4. 耗尽后：留盘、归零、抽不到 ---")
        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        names = [s["name"] for s in secs]
        ok("扇区仍在盘上（不下架）", "奖品甲" in names, names)
        sa = [s for s in secs if s["name"] == "奖品甲"][0]
        ok("奖品甲概率静默归零", abs(sa["prob"]) < 1e-6, sa)
        ok("用户端无 sold_out 字段（看不出已抢完）", "sold_out" not in sa, sa)

        ids2 = ["V%03d" % i for i in range(1, 21)]
        add_users(ck, "\n".join(ids2))
        a2 = 0
        for uid in ids2:
            st, d, _ = call("/api/user/spin", {"uid": uid})
            if d.get("is_win") and d.get("prize_name") == "奖品甲":
                a2 += 1
        ok("耗尽后 20 次抽奖，奖品甲 0 次中出", a2 == 0, a2)

        print("\n--- 5. 测试ID 不受库存限制 ---")
        # 把奖品甲概率拉到 100，其他归零；库存仍是 2（已发满）
        save_prizes(ck, [
            {"id": p1["id"], "name": "奖品甲", "code": "P001", "prob": 100, "stock": 2},
            {"id": p2["id"], "name": "奖品乙", "code": "P002", "prob": 0, "stock": -1},
            {"id": p3["id"], "name": "奖品丙", "code": "P003", "prob": 0, "stock": 100},
        ])
        add_users(ck, "W001\nW002")
        w_hit = 0
        for uid in ("W001", "W002"):
            st, d, _ = call("/api/user/spin", {"uid": uid})
            if d.get("is_win"):
                w_hit += 1
        ok("正式ID：库存耗尽后 0 次中奖", w_hit == 0, w_hit)

        add_users(ck, "T001\nT002", is_test=True)
        t_hit = 0
        for uid in ("T001", "T002"):
            st, d, _ = call("/api/user/spin", {"uid": uid})
            if d.get("is_win") and d.get("is_test") == 1 and d.get("prize_name") == "奖品甲":
                t_hit += 1
        ok("测试ID：正式库存抽光后仍能中奖", t_hit == 2, t_hit)

        st, d, _ = call("/api/admin/prizes", cookie=ck)
        pa = [x for x in d["items"] if x["name"] == "奖品甲"][0]
        ok("测试中奖不计入正式已发放", pa["used"] == 2, pa)

        print("\n--- 6. 概率和 >100 时压缩 ---")
        save_prizes(ck, [
            {"name": "A", "code": "C1", "prob": 60, "stock": -1},
            {"name": "B", "code": "C2", "prob": 60, "stock": -1},
        ])
        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        ok("概率和压回 100", abs(sum(s["prob"] for s in secs) - 100) < 0.01, [s["prob"] for s in secs])
        ok("压缩后无谢谢参与（必中）", all(s["is_win"] for s in secs), [s["is_win"] for s in secs])

        print("\n--- 7. 概率全 0 ---")
        save_prizes(ck, [{"name": "A", "code": "C1", "prob": 0, "stock": -1}])
        st, d, _ = call("/api/user/prizes")
        secs = d["sectors"]
        ok("概率全 0：奖品留盘但概率 0 + 谢谢参与 100",
           [s["name"] for s in secs] == ["A", "谢谢参与"] and secs[0]["prob"] == 0 and abs(secs[1]["prob"] - 100) < 1e-6,
           secs)

        print("\n--- 8. 发货表 E 列 = 奖品编号 ×1 ---")
        save_prizes(ck, [{"name": "保温杯", "code": "B001", "prob": 100, "stock": -1}])
        add_users(ck, "S001")
        st, d, _ = call("/api/user/spin", {"uid": "S001"})
        ok("S001 必中保温杯", d.get("is_win") == 1 and d.get("prize_name") == "保温杯", d)
        ok("响应带 prize_code=B001", d.get("prize_code") == "B001", d)
        st, d, _ = call("/api/user/shipping", {"uid": "S001", "name": "张三", "phone": "13800001111",
                                               "province": "广东省", "city": "深圳市",
                                               "district": "南山区", "address": "科技园路1号"})
        ok("提交收货信息成功", d.get("ok") is True, d)

        st, raw, h = call("/api/admin/export/shipping.csv", cookie=ck, raw=True)
        ok("发货表带 UTF-8 BOM", raw[:3] == b"\xef\xbb\xbf", raw[:6])
        text = raw.decode("utf-8")
        lines = [l for l in text.strip().splitlines() if l.strip()]
        ok("发货表表头 11 列", len(lines[0].split(",")) == 11, lines[0])
        body = lines[1] if len(lines) > 1 else ""
        ok("E 列 = B001*1", ",B001*1," in body, body)
        ok("B 列 = 部门编号 1000003263", "1000003263" in body, body)
        ok("C/D 列固定文案", body.count("双节课消活动礼品") == 2, body)
        ok("F/G/H 省市区", ("广东省" in body) and ("深圳市" in body) and ("南山区" in body), body)

        print("\n--- 9. 看板 / 记录 ---")
        st, d, _ = call("/api/admin/overview", cookie=ck)
        s = d["stats"]
        ok("看板返回 prizes 列表", isinstance(s.get("prizes"), list) and len(s["prizes"]) == 1, s.get("prizes"))
        ok("看板 prize_count=1", s.get("prize_count") == 1, s)
        st, d, _ = call("/api/admin/records", cookie=ck)
        it = d["items"][0] if d.get("items") else {}
        ok("记录带 prize_code 字段", "prize_code" in it, list(it.keys()))

        print("\n--- 10. 幂等：同一ID只能抽一次 ---")
        st, d, _ = call("/api/user/spin", {"uid": "S001"})
        ok("重复抽奖被拦", d.get("status") in ("already_drawn", "need_shipping"), d)
        st, d, _ = call("/api/user/login", {"uid": "S001"})
        ok("已填收货信息的ID登录返回 already_drawn", d.get("status") == "already_drawn", d)

        print("\n--- 11. 允许清空奖品表 ---")
        st, d, _ = save_prizes(ck, [])
        ok("保存空列表成功", d.get("saved") == 0, d)
        st, d, _ = call("/api/admin/prizes", cookie=ck)
        ok("奖品表已清空", d.get("items") == [], d)
        st, d, _ = call("/api/user/prizes")
        ok("清空后转盘只剩谢谢参与", [x["name"] for x in d["sectors"]] == ["谢谢参与"], d["sectors"])

        print("\n--- 12. 其他基础检查 ---")
        st, d, _ = call("/api/admin/overview")
        ok("未登录访问看板 = 401", st == 401, st)
        st, d, _ = call("/api/regions")
        ok("省市区数据可拉取（34 省）", st == 200 and isinstance(d, list) and len(d) == 34,
           (st, len(d) if isinstance(d, list) else None))
        st, raw, h = call("/api/admin/records/export.csv", cookie=ck, raw=True)
        ok("明细CSV 带 BOM", raw[:3] == b"\xef\xbb\xbf", raw[:6])
        head = raw.decode("utf-8").strip().splitlines()[0]
        ok("明细CSV 14 列", len(head.split(",")) == 14, head)

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
        print("  服务日志留在：%s" % TMP)
        print("=" * 58)
        return 1
    print("  通过 %d 项，失败 0 项" % PASSED)
    print("=" * 58)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
