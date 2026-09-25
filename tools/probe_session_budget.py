#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话额度探针：写接口被限流，到底是"写太快"还是"这一整个会话总共请求太多"？

背景（2026-09-25 实测矛盾）：
    * 全新会话里以 1 req/s 打 25 发写请求 —— 全程没事。
    * 全新会话里以 2 req/s 打 25 发写请求 —— 全程没事。
    * 但 `grab.py --live --now` 在第 2~3 发写请求就被限流并作废会话。

两者的区别不在写速率，而在**写之前的预检**：真跑的时候，会话在放课前一秒内已经
发过 ~34 个请求，其中对时那一步就是 25 个 GET 连发（`server_offset`，间隔 20ms，
≈16 req/s）。所以怀疑限流是**整个会话的请求额度**，读请求也在扣。

做法：每一轮都换全新会话，先按不同方式做只读请求，紧接着打 1 发写请求（假教学班 ID），
看它会不会被限流、会话会不会死。

安全：写请求的 teachingClassId 恒为不存在的假 ID，operationType 恒为 "1"，无退选路径。

    python3 session_budget_probe.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent          # tools/ 的上一级才是仓库根目录（grab.py / school_auth.py 在那）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import school_auth as A          # noqa: E402
import grab as G          # noqa: E402

FAKE_ID = "999999999999999999999"
REAL_TC = "000000000000000000000001"     # 真实候选，但只用来做**只读**容量查询


def log(msg: str) -> None:
    print(msg, flush=True)


def fresh(creds, solver, school) -> None:
    school.adopt(A.Login(creds, solver, captcha_attempts=4, log=lambda m: None).login())


def read_burst(school, kind: str, n: int, gap: float) -> tuple[int, float]:
    """按指定方式打 n 个只读请求，返回 (成功数, 总耗时)。"""
    ok = 0
    t0 = time.time()
    for i in range(n):
        try:
            if kind == "clock":            # 与 server_offset 完全一样：GET 站点轻量页 ?_=
                school.date_sample()
            elif kind == "capacity":       # 业务只读 API
                school.capacity(REAL_TC, BATCH)
            elif kind == "student":        # 探活接口
                school.student(CODE, recover=False)
            ok += 1
        except Exception:  # noqa: BLE001
            pass
        if gap and i < n - 1:
            time.sleep(gap)
    return ok, time.time() - t0


def try_write(school) -> tuple[str, str, float]:
    payload, status, text, dt = school.submit(CODE, BATCH, FAKE_ID, "01")
    verdict, msg = G.classify(payload, status, text)
    return verdict, msg, dt


ROUNDS = [
    # (标签, 只读类型, 数量, 间隔)
    ("对照：不打任何只读", None, 0, 0.0),
    ("对时 25 发（现预检行为）", "clock", 25, 0.02),
    ("对时 25 发（0.3s 间隔）", "clock", 25, 0.30),
    ("对时 45 发（0.02s 间隔）", "clock", 45, 0.02),
    ("业务读 25 发 capacity", "capacity", 25, 0.02),
    ("探活 25 发 student", "student", 25, 0.02),
]

CODE = ""
BATCH = ""


def main() -> int:
    global CODE, BATCH
    # 这些探针都会用真实账号登录 —— 学校同一账号只允许一个有效会话，
    # 真跑着 grab 的时候再跑探针会把它的会话顶掉。
    if not G.acquire_instance_lock():
        return 2
    creds = A.load_credentials()
    if creds is None:
        log("✗ 需要凭据文件")
        return 2
    CODE = creds.student_id
    solver = A.CaptchaSolver()
    school = G.School("", "", "")
    school.code = CODE

    log("=" * 78)
    log("  会话额度探针：只读请求会不会把写接口的额度用掉？")
    log("=" * 78)

    rows = []
    for label, kind, n, gap in ROUNDS:
        log("")
        log(f"── {label} ──")
        try:
            fresh(creds, solver, school)
        except A.AuthError as exc:
            log(f"  ✗ 登录失败: {exc}")
            continue
        d = (school.student(CODE).get("data") or {})
        BATCH = str((d.get("electiveBatch") or {}).get("code") or "")
        taken, secs = (0, 0.0)
        if kind and n:
            taken, secs = read_burst(school, kind, n, gap)
            log(f"  只读 {taken}/{n} 发完成，耗时 {secs:.2f}s（≈{taken / max(secs, .001):.1f} req/s）")
        verdict, msg, dt = try_write(school)
        alive, detail = school.probe()
        bad = verdict in (G.V_BUSY, G.V_EXPIRED)
        log(f"  紧接着的写请求 → {verdict}  {msg[:44]}  ({dt:.0f}ms)")
        log(f"  会话：{'仍在' if alive else '已失效'}  {detail.get('msg', '')[:30]}")
        rows.append({"label": label, "kind": kind, "reads": taken, "read_secs": round(secs, 2),
                     "verdict": verdict, "msg": msg[:60], "write_ms": round(dt, 1),
                     "session_alive": alive})
        time.sleep(1.0)          # 让上一轮的限流痕迹先散掉

    log("")
    log("=" * 78)
    log("  汇总")
    log("=" * 78)
    log(f"  {'只读前置':<26} {'只读':>5} {'耗时':>7} {'写请求结果':<14} {'会话':>5}")
    for r in rows:
        log(f"  {r['label']:<26} {r['reads']:>5} {r['read_secs']:>6.2f}s "
            f"{r['verdict']:<14} {'在' if r['session_alive'] else '失效':>5}")
    out = Path("rate-lab")
    out.mkdir(exist_ok=True)
    path = out / f"session_budget_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"  记录: {path}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
