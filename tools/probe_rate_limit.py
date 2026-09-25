#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写接口限流实验台：自动登录 + 可控节奏 + 逐发记录。

安全设计（硬约束）：
    * operationType 恒为 "1"（加课志愿）。本文件不存在 "2"（退选）。
    * teachingClassId 恒为**不存在的假 ID**，服务端写库前就拒掉，
      结构上不可能选中任何课，也碰不到你已有的课。
    * 每一级实验都用**全新会话**（自动登录），只测"这一级节奏能打几发"。
    * 一撞限流立即停手，绝不在同一会话里继续加压。

要回答的问题：
    1. 某个固定间隔下，第几发会被限流？（速率曲线）
    2. 限流之后会话还在吗？
    3. 换一个新会话，额度会不会重置？（决定"要不要主动重登录续命"）

    python3 rate_limit_probe.py --gaps 2.0,1.0,0.5 --writes 14
    python3 rate_limit_probe.py --gaps 0.75 --writes 30 --reads-between 1
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent          # tools/ 的上一级才是仓库根目录（grab.py / school_auth.py 在那）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import school_auth as A          # noqa: E402
import grab as G          # noqa: E402

FAKE_ID = "999999999999999999999"      # 不存在的教学班：写不进任何东西
THROTTLE = (G.V_BUSY, G.V_EXPIRED)


def log(msg: str) -> None:
    print(msg, flush=True)


def fresh_session(creds, solver, school, reason: str) -> float:
    """强制来一次全新登录（不走 recover 的"先探活"捷径），返回耗时秒数。"""
    t0 = time.time()
    session = A.Login(creds, solver, captcha_attempts=4, log=lambda m: None).login()
    school.adopt(session)
    return time.time() - t0


def session_state(school, code: str) -> tuple[bool, str]:
    ok, payload = school.probe()
    return ok, str(payload.get("msg") or payload.get("code") or "")


def run_level(school, code: str, batch: str, campus: str, gap: float,
              writes: int, reads_between: bool, tc_id: str, require_full: bool,
              real_ok) -> dict:
    """一个间隔级别：固定节奏打到第一发限流为止。

    `require_full=True` 时（用真实教学班 ID），每发之前先用只读容量接口确认它仍然满员；
    一旦出现空位就立刻停手 —— 探针不该替你抢课，那件事交给 grab.py。
    """
    marks: list[dict] = []
    t0 = time.time()
    throttled_at = None
    stopped = ""
    for i in range(1, writes + 1):
        if require_full:
            try:
                cap = school.capacity(tc_id, batch)
                free = (int(cap.get("nonMainClassCapacity") or 0)
                        - int(cap.get("nonMainElectiveNumber") or 0))
            except Exception as exc:  # noqa: BLE001
                stopped = f"容量查询失败: {exc}"
                break
            if free > 0:
                stopped = f"⚠ {tc_id} 出现 {free} 个空位，探针立即停手"
                break
        target = t0 + (i - 1) * gap
        wait = target - time.time()
        if wait > 0:
            time.sleep(wait)
        t_send = time.time()
        payload, status, text, dt = school.submit(code, batch, tc_id, campus)
        verdict, msg = G.classify(payload, status, text)
        marks.append({"i": i, "t": round(t_send - t0, 3), "dt": round(dt, 1),
                      "code": str(payload.get("code")), "verdict": verdict, "msg": msg[:60]})
        flag = "⚠" if verdict in THROTTLE else " "
        log(f"    {flag} #{i:>2} T+{t_send - t0:6.3f}s {dt:6.0f}ms code={payload.get('code')} "
            f"{verdict:<14} {msg[:44]}")
        if verdict in THROTTLE:
            throttled_at = i
            break
        if reads_between:
            try:
                school.capacity(tc_id, batch)
            except Exception:  # noqa: BLE001
                pass
    alive, detail = session_state(school, code)
    rtts = [m["dt"] for m in marks]
    return {"gap": gap, "writes": len(marks), "throttled_at": throttled_at,
            "session_alive": alive, "session_msg": detail,
            "rtt_med": round(statistics.median(rtts), 1) if rtts else None,
            "tc": tc_id, "stopped": stopped, "marks": marks}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gaps", default="2.0,1.0,0.5",
                    help="逗号分隔的固定间隔秒数（每一级换一个新会话）")
    ap.add_argument("--writes", type=int, default=14, help="每级最多打几发")
    ap.add_argument("--reads-between", type=int, default=0,
                    help="每发写之后再打一次只读 capacity.do，用来测只读是否占额度")
    ap.add_argument("--id-mode", choices=("fake", "real"), default="fake",
                    help="fake=假教学班（走不到真实写路径，只测通用频率墙）；"
                         "real=真实候选（每发前先确认它仍然满员，出现空位立刻停手）")
    ap.add_argument("--tc", default="000000000000000000000001", help="--id-mode real 时用的教学班")
    ap.add_argument("--out", default="rate-lab", help="逐发记录的落盘目录")
    args = ap.parse_args()

    # 这些探针都会用真实账号登录 —— 学校同一账号只允许一个有效会话，
    # 真跑着 grab 的时候再跑探针会把它的会话顶掉。
    if not G.acquire_instance_lock():
        return 2
    creds = A.load_credentials()
    if creds is None:
        log("✗ 需要 ~/.config/course-grabber/credentials.json")
        return 2
    solver = A.CaptchaSolver()
    school = G.School("", "", "")
    school.code = creds.student_id
    school.auth = A.ReloginManager(creds, solver, max_logins=99, min_gap=2.0,
                                   log=lambda m: None)

    log("=" * 78)
    log("  写接口限流实验台  volunteer.do  （假教学班 ID，结构上不可能选中任何课）")
    log("=" * 78)
    log(f"  账号 {creds.student_id[:4]}****{creds.student_id[-2:]}   假ID {FAKE_ID}")
    log(f"  计划：间隔 {args.gaps}   每级 {args.writes} 发"
        f"{'   写后加一发只读' if args.reads_between else ''}")

    results = []
    for spec in [s for s in args.gaps.split(",") if s.strip()]:
        gap = float(spec)
        log("")
        log(f"── 间隔 {gap}s（≈{1 / gap:.2f} req/s），换全新会话 ──")
        try:
            took = fresh_session(creds, solver, school, "实验台")
        except A.AuthError as exc:
            log(f"  ✗ 登录失败，跳过: {exc}")
            continue
        year = school.student(creds.student_id)
        d = year.get("data") or {}
        batch = str((d.get("electiveBatch") or {}).get("code") or "")
        campus = str(d.get("campus") or "01")
        log(f"  新会话就绪（{took:.1f}s），批次 {batch} 校区 {campus}")
        tc_id = FAKE_ID if args.id_mode == "fake" else args.tc
        r = run_level(school, creds.student_id, batch, campus, gap,
                      args.writes, bool(args.reads_between), tc_id,
                      args.id_mode == "real", None)
        if r.get("stopped"):
            log(f"  ⚠ {r['stopped']}")
        results.append(r)
        log(f"  ⇒ 打了 {r['writes']} 发，"
            f"{'第 ' + str(r['throttled_at']) + ' 发被限流' if r['throttled_at'] else '全程未被限流'}"
            f"；会话{'仍在' if r['session_alive'] else '已失效'}（{r['session_msg'][:30]}）"
            f"；RTT 中位 {r['rtt_med']}ms")

    log("")
    log("=" * 78)
    log("  汇总")
    log("=" * 78)
    log(f"  {'教学班':<24} {'间隔':>6} {'速率':>9} {'打了几发':>8} {'首限流':>7} {'会话':>6} {'RTT中位':>8}")
    for r in results:
        log(f"  {r['tc'][-12:]:<24} {r['gap']:>6.2f} {1 / r['gap']:>6.2f}/s {r['writes']:>8} "
            f"{str(r['throttled_at'] or '-'):>7} "
            f"{'在' if r['session_alive'] else '失效':>6} {r['rtt_med']:>6.0f}ms")
    os.makedirs(args.out, exist_ok=True)
    path = Path(args.out) / f"level_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"  逐发记录: {path}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
