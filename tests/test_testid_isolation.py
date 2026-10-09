"""验证「测试ID 不消耗正式库存」+「库存耗尽后正式ID 抽不到」。"""
import os, sys, time, tempfile, json, shutil, subprocess, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # lottery/ 目录
TMP = tempfile.mkdtemp(prefix="lot_t2_")
os.environ["LOTTERY_DATA_DIR"] = TMP
sys.path.insert(0, os.path.join(ROOT))
import app

PORT = 18583
app.init_db()
c = app.get_db()
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT INTO prizes(name,code,prob,stock,sort) VALUES ('礼品A','A001',100.0,3,0)")   # 必中，正式库存只有 3
for u in ["R1", "R2", "R3", "R4", "R5"]:
    c.execute("INSERT INTO users(uid,added_at,is_test) VALUES (?,?,0)", (u, app.now_str()))
for u in ["T1", "T2", "T3", "T4"]:
    c.execute("INSERT INTO users(uid,added_at,is_test) VALUES (?,?,1)", (u, app.now_str()))
c.execute("COMMIT")
app.put_db(c)

srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), "--port", str(PORT)],
                       env=dict(os.environ, PORT=str(PORT), HOST="127.0.0.1"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = "http://127.0.0.1:%d" % PORT
try:
    for _ in range(100):
        try:
            op.open(base + "/api/health", timeout=1).read(); break
        except Exception:
            time.sleep(0.15)

    def post(p, d):
        r = urllib.request.Request(base + p, data=json.dumps(d).encode(),
                                   headers={"Content-Type": "application/json"})
        return json.loads(op.open(r, timeout=10).read().decode())

    print("正式库存 = 3；5 个正式ID + 4 个测试ID")
    print("--- 正式 ID 依次抽 ---")
    for u in ["R1", "R2", "R3", "R4", "R5"]:
        r = post("/api/user/spin", {"uid": u})
        print("  %-3s 中奖=%d  奖项=%s" % (u, r.get("is_win", -1), r.get("prize_name")))
    print("--- 测试 ID 依次抽（此时正式库存已耗尽）---")
    for u in ["T1", "T2", "T3", "T4"]:
        r = post("/api/user/spin", {"uid": u})
        print("  %-3s 中奖=%d  is_test=%s" % (u, r.get("is_win", -1), r.get("is_test")))

    c2 = app.get_db()
    real = c2.execute("SELECT COUNT(*) FROM records WHERE is_win=1 AND COALESCE(is_test,0)=0").fetchone()[0]
    test = c2.execute("SELECT COUNT(*) FROM records WHERE is_win=1 AND COALESCE(is_test,0)=1").fetchone()[0]
    app.put_db(c2)
    print()
    print("  正式中奖 = %d（应为 3）" % real)
    print("  测试中奖 = %d（应为 4）" % test)
    ok = real == 3 and test == 4
    print("  %s" % ("PASS 测试ID 完全隔离：不消耗正式库存，正式库存耗尽后仍可中奖（仅供测试）"
                    if ok else "FAIL"))
    sys.exit(0 if ok else 1)
finally:
    srv.terminate()
    try: srv.wait(timeout=10)
    except Exception: srv.kill()
    shutil.rmtree(TMP, ignore_errors=True)
