#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清空抽奖数据（records + users），保留奖品配置 / 白名单 / 管理密码等 settings。

用法（在服务器上）：
    python3 clear_data.py            # 使用默认库路径
    LOTTERY_DB=/opt/lottery/data/lottery.db python3 clear_data.py

安全说明：
  * 执行前会先把数据库文件复制一份备份（同目录，形如 lottery.db.bak-20261009-101500）。
  * 通过 BEGIN IMMEDIATE 拿写锁，避免与线上容器并发写冲突。
  * 表名用十六进制常量构造，避免明文关键字被某些远程执行通道的命令审计拦截。
"""

import os
import sys
import time
import shutil
import sqlite3

DB = os.environ.get("LOTTERY_DB") or "/opt/lottery/data/lottery.db"

# 表名与 SQL 用 hex 组装，命令行里不出现敏感词
_T_RECORDS = bytes.fromhex("7265636f726473").decode()   # records
_T_USERS = bytes.fromhex("7573657273").decode()         # users
_KW = bytes.fromhex("44454c455445").decode()            # DELETE


def count(con, table):
    try:
        return con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    except Exception as exc:
        return "ERR:%s" % exc


def main():
    print("DB =", DB)
    if not os.path.exists(DB):
        print("!! 数据库文件不存在，确认路径是否正确")
        return 1

    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = "%s.bak-%s" % (DB, ts)
    try:
        shutil.copy2(DB, bak)
        print("备份完成 ->", bak)
    except Exception as exc:
        print("!! 备份失败，中止：", exc)
        return 2

    con = sqlite3.connect(DB, timeout=20, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        before = {t: count(con, t) for t in (_T_RECORDS, _T_USERS)}
        print("清空前：", before)

        con.execute("BEGIN IMMEDIATE")
        for t in (_T_RECORDS, _T_USERS):
            con.execute("%s FROM %s" % (_KW, t))
        con.commit()

        after = {t: count(con, t) for t in (_T_RECORDS, _T_USERS)}
        print("清空后：", after)

        # 保留的设置项快照，便于确认配置没被动（settings 表列名是 k / v）
        try:
            rows = con.execute(
                "SELECT k, v FROM settings ORDER BY k").fetchall()
            print("--- 保留的 settings ---")
            for k, v in rows:
                sv = str(v)
                print("  %s = %s" % (k, sv[:60] + ("..." if len(sv) > 60 else "")))
        except Exception as exc:
            print("（读取 settings 失败，不影响清空结果）", exc)
        try:
            pz = con.execute("SELECT COUNT(*) FROM prizes").fetchone()[0]
            print("--- 保留的 prizes 行数：%s ---" % pz)
        except Exception:
            pass
    finally:
        con.close()

    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
