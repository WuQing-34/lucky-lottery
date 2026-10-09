"""需求4 承载能力验证：600 并发抽奖 + 库存不超发 + 无重复中奖。

要点：
  · 绕开沙箱 HTTP_PROXY（否则 600 并发会被环境代理掐掉，得到假结论）
  · 奖品库存设成小于并发数（例如 300），验证「恰好发满、绝不超发」
  · 用「记录条数 = 独立 ID 数」验证 uid UNIQUE 约束真实生效
"""
import os, sys, time, tempfile, shutil, json, threading, collections
import urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # lottery/ 目录
TMP = tempfile.mkdtemp(prefix="lot_stress600_")
os.environ["LOTTERY_DATA_DIR"] = TMP
sys.path.insert(0, os.path.join(ROOT))
import app
import subprocess

PORT = 18581
STOCK = 300          # 故意小于并发数，逼出超发 bug
CONC = 600

app.init_db()
c = app.get_db()
c.execute("BEGIN IMMEDIATE")
# 一个必中奖品：概率 100%、库存 = STOCK（故意小于并发数，逼出超发 bug）
c.execute("INSERT INTO prizes(name, code, prob, stock, sort) VALUES (?,?,?,?,?)",
          ("限量周边", "G001", 100.0, STOCK, 0))
uids = ["S%04d" % i for i in range(CONC)]
for u in uids:
    c.execute("INSERT INTO users(uid, added_at) VALUES (?,?)", (u, app.now_str()))
c.execute("COMMIT")
app.put_db(c)

srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py"), "--port", str(PORT)],
                       env=dict(os.environ, PORT=str(PORT), HOST="127.0.0.1"),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = "http://127.0.0.1:%d" % PORT
try:
    for _ in range(100):
        try:
            opener.open(base + "/api/health", timeout=1).read(); break
        except Exception:
            time.sleep(0.15)

    def post(path, payload):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(opener.open(req, timeout=20).read().decode())

    print("奖品库存 = %d，并发 = %d（库存故意小于并发，检验不超发）" % (STOCK, CONC))
    results = {}
    lock = threading.Lock()
    errs = []

    def worker(uid):
        try:
            r = post("/api/user/spin", {"uid": uid})
            with lock:
                results[uid] = r
        except Exception as e:
            with lock:
                errs.append("%s: %s" % (type(e).__name__, e))

    # 同时起跑：用 barrier 保证 600 个请求真正挤在同一瞬间
    barrier = threading.Barrier(CONC, timeout=30)
    def sync_worker(uid):
        try:
            barrier.wait()
        except Exception:
            pass
        worker(uid)

    ths = [threading.Thread(target=sync_worker, args=(u,)) for u in uids]
    t0 = time.perf_counter()
    for t in ths: t.start()
    for t in ths: t.join()
    dur = time.perf_counter() - t0

    wins = [u for u, r in results.items() if r.get("is_win") == 1]
    loses = [u for u, r in results.items() if r.get("is_win") == 0]
    noluck = [u for u, r in results.items() if "is_win" not in r]
    won_names = collections.Counter(r.get("prize_name") for r in results.values() if r.get("is_win"))

    c2 = app.get_db()
    db_wins = c2.execute("SELECT COUNT(*) FROM records WHERE is_win=1").fetchone()[0]
    db_total = c2.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    db_code = c2.execute(
        "SELECT COUNT(*) FROM records WHERE is_win=1 AND TRIM(COALESCE(prize_code,''))<>''"
    ).fetchone()[0]
    dup = c2.execute("SELECT COUNT(*) FROM (SELECT uid FROM records GROUP BY uid HAVING COUNT(*)>1)").fetchone()[0]
    app.put_db(c2)

    print()
    print("耗时            : %.2f s（%.0f 抽/秒）" % (dur, CONC / dur))
    print("HTTP 层失败      : %d  %s" % (len(errs), errs[:2]))
    print("响应成功收到     : %d / %d" % (len(results), CONC))
    print("中奖            : %d" % len(wins))
    print("未中奖          : %d" % len(loses))
    print("无 is_win 字段   : %d" % len(noluck))
    print("DB 中奖记录      : %d" % db_wins)
    print("DB 记录总数      : %d" % db_total)
    print("DB 带编号中奖数  : %d" % db_code)
    print("重复 uid 记录数  : %d" % dup)
    print("中出奖品分布    : %s" % dict(won_names))
    print()
    ok = True
    def chk(name, cond):
        global ok
        print("  %s  %s" % ("PASS" if cond else "FAIL", name))
        if not cond: ok = False
    chk("HTTP 无失败", len(errs) == 0)
    chk("600 个请求全部收到响应", len(results) == CONC)
    chk("中奖数恰好等于库存 %d（不超发、不少发）" % STOCK, len(wins) == STOCK)
    chk("DB 中奖记录 = 库存", db_wins == STOCK)
    chk("DB 记录总数 = 600（每 ID 一条）", db_total == CONC)
    chk("无重复 uid", dup == 0)
    chk("中奖记录都带上了物料编号", db_code == STOCK)
    if STOCK < CONC:
        chk("库存耗尽后剩余 %d 人全部未中奖" % (CONC - STOCK), len(loses) == CONC - STOCK)
    print()
    print("===== %s =====" % ("全部通过" if ok else "存在失败项"))
    sys.exit(0 if ok else 1)
finally:
    srv.terminate()
    try: srv.wait(timeout=10)
    except Exception: srv.kill()
    shutil.rmtree(TMP, ignore_errors=True)
