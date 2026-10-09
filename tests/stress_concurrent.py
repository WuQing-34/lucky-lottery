# -*- coding: utf-8 -*-
"""并发压测：验证「奖品库存 N、同时来 M 个请求」不超发，并测 600 峰值下的响应表现。
用法：python stress_concurrent.py [并发数] [奖品库存]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
PORT = 8799
BASE = "http://127.0.0.1:%d" % PORT

N = int(sys.argv[1]) if len(sys.argv) > 1 else 600
STOCK = int(sys.argv[2]) if len(sys.argv) > 2 else 600

# 关键：显式绕开环境里的 HTTP_PROXY，并且 **全脚本复用同一个 opener**。
# 沙箱代理对单机连接数有上限（约 260），不绕开会得到假的 503 proxy overloaded；
# 而每次请求都 build_opener(...) 也极贵（实测 83ms/次 vs 复用 5.9ms/次），
# 会把「客户端开销」误读成「服务端慢」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(path, body=None, cookie=None, timeout=60):
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=h)
    try:
        with _OPENER.open(r, timeout=timeout) as x:
            return x.status, json.loads(x.read().decode("utf-8")), dict(x.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, {"raw": raw}, dict(e.headers)
    except Exception as e:
        return 0, {"raw": "EXC %s" % e}, {}


def main():
    tmp = tempfile.mkdtemp(prefix="lottery_stress_")
    env = dict(os.environ)
    env.update({"PORT": str(PORT), "HOST": "127.0.0.1", "LOTTERY_DATA_DIR": tmp})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        env.pop(k, None)
    logf = open(os.path.join(tmp, "srv.log"), "wb")
    proc = subprocess.Popen([sys.executable, APP], env=env, stdout=logf, stderr=subprocess.STDOUT)

    try:
        for _ in range(80):
            time.sleep(0.25)
            if req("/api/health")[0] == 200:
                break
        else:
            raise RuntimeError("服务未启动")

        st, d, h = req("/api/admin/login", {"password": "admin888"})
        ck = [v for k, v in h.items() if k.lower() == "set-cookie"][0].split(";")[0]

        req("/api/admin/settings", {"whitelist_only": False}, cookie=ck)
        req("/api/admin/prizes", {"items": [
            {"name": "限量周边", "code": "G001", "prob": 100, "stock": STOCK}]}, cookie=ck)

        # ---------- 并发：N 个不同 ID 同时抽奖，库存 = STOCK ----------
        results = [None] * N
        t0 = time.time()
        barrier = threading.Barrier(N)

        def worker(i):
            uid = "p%04d" % i
            try:
                barrier.wait(timeout=60)
            except Exception:
                pass
            s, body, _ = req("/api/user/spin", {"uid": uid}, timeout=90)
            results[i] = (s, body)

        # 先建号（并发前的成本，不计入抽奖耗时）
        t_pre = time.time()
        for i in range(0, N, 200):
            batch = ["p%04d" % j for j in range(i, min(i + 200, N))]
            req("/api/admin/users", {"text": "\n".join(batch), "mode": "append", "is_test": False}, cookie=ck)
        pre_ms = (time.time() - t_pre) * 1000

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        wall = time.time() - t0

        okc = sum(1 for r in results if r and r[0] == 200 and r[1].get("status") == "ok")
        errs = [r for r in results if not r or r[0] != 200]
        wins = sum(1 for r in results if r and r[1].get("is_win"))
        loses = sum(1 for r in results if r and r[0] == 200 and r[1].get("status") == "ok"
                    and not r[1].get("is_win"))
        dup = sum(1 for r in results if r and r[1].get("status") == "already_drawn")

        print("=" * 62)
        print("  并发压测：%d 个不同ID 同时抽奖，奖品库存 = %d" % (N, STOCK))
        print("-" * 62)
        print("  预建号耗时        : %.0f ms（%d 个ID，分 %d 批）" % (pre_ms, N, (N + 199) // 200))
        print("  并发整体耗时      : %.2f s" % wall)
        print("  吞吐              : %.0f req/s" % (N / wall if wall else 0))
        print("  成功响应(ok)      : %d" % okc)
        print("  中奖 / 未中奖     : %d / %d" % (wins, loses))
        print("  重复(already)     : %d" % dup)
        print("  非 200 响应       : %d" % len(errs))
        if errs:
            print("  错误样本          : %s" % errs[:3])
        print("-" * 62)
        verdict = []
        verdict.append(("不超发", wins <= STOCK))
        verdict.append(("库存恰好用满", wins == min(STOCK, N)))
        verdict.append(("无失败请求", len(errs) == 0))
        verdict.append(("无重复吞号", dup == 0))
        for name, good in verdict:
            print("  %s %s" % ("PASS" if good else "FAIL", name))
        print("=" * 62)

        # ---------- 读接口压测（看板 + 转盘配置） ----------
        t1 = time.time()
        cnt = 300
        done = []

        def reader(i):
            s, b, _ = req("/api/admin/overview", cookie=ck)
            done.append(s)

        ths = [threading.Thread(target=reader, args=(i,)) for i in range(cnt)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=120)
        w2 = time.time() - t1
        print("  看板并发 %d 次：%.2f s（%.0f req/s），全部 200 = %s"
              % (cnt, w2, cnt / w2 if w2 else 0, all(x == 200 for x in done)))

        return 0 if all(g for _, g in verdict) else 1

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
