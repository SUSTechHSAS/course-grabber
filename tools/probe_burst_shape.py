#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""突发形状探针：一个会话里"一秒钟内连发几发写请求"才会被限流。

为什么需要它：
    之前的结论（"每滚动 1 秒最多 3 发"）是用**假教学班 ID** 测的，而假 ID 在服务端
    写库前就被拒了；真实候选走的是另一条更贵的路径。2026-09-25 早的真实 --live
    运行在第 2~3 发就被限流，与那个结论矛盾，所以要用真实（且满员的）教学班重测。

安全：
    * operationType 恒为 "1"（无退选路径）。
    * 只用**当前已满**的真实候选班；每发之前先用只读接口确认仍然满员，
      一旦出现空位立刻停手（抢课是 grab.py 的事，不是探针的事）。
    * 每一轮换全新会话；撞限流立刻结束该轮。

    python3 burst_shape_probe.py --levels 1,2,3,4
    python3 burst_shape_probe.py --levels 3 --gap 0.0 --repeat 2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import school_auth as A          # noqa: E402
import grab as G          # noqa: E402

# 五个真实候选（周三 3-5 四个互相冲突，周五一个）。全部只用只读接口确认满员后才写。
CANDS = [tc for tc, _lab, _g in G.DEFAULT_CANDIDATES]
THROTTLE = (G.V_BUSY, G.V_EXPIRED)


def log(msg: str) -> None:
    print(msg, flush=True)


def free_slots(school, tc: str, batch: str) -> int:
    cap = school.capacity(tc, batch)
    return (int(cap.get("nonMainClassCapacity") or 0)
            - int(cap.get("nonMainElectiveNumber") or 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2,3,4", help="每轮连发几发（逗号分隔）")
    ap.add_argument("--gap", type=float, default=0.0, help="连发之间的间隔，默认 0")
    ap.add_argument("--repeat", type=int, default=1, help="每个级别重复几轮")
    ap.add_argument("--out", default="rate-lab")
    args = ap.parse_args()

    # 这些探针都会用真实账号登录 —— 学校同一账号只允许一个有效会话，
    # 真跑着 grab 的时候再跑探针会把它的会话顶掉。
    if not G.acquire_instance_lock():
        return 2
    creds = A.load_credentials()
    if creds is None:
        log("✗ 需要凭据文件")
        return 2
    solver = A.CaptchaSolver()
    school = G.School("", "", "")
    school.code = creds.student_id

    log("=" * 78)
    log("  突发形状探针（真实满员教学班，operationType=1，无退选路径）")
    log("=" * 78)

    results = []
    for level in [int(x) for x in args.levels.split(",") if x.strip()]:
        for rep in range(args.repeat):
            log("")
            log(f"── 连发 {level} 发（间隔 {args.gap}s）第 {rep + 1}/{args.repeat} 轮 ──")
            school.adopt(A.Login(creds, solver, captcha_attempts=4,
                                 log=lambda m: None).login())
            d = school.student(creds.student_id).get("data") or {}
            batch = str((d.get("electiveBatch") or {}).get("code") or "")
            if not batch:
                log("  ✗ 拿不到批次码")
                continue

            t0 = time.time()
            marks = []
            hit = None
            for i in range(level):
                tc = CANDS[i % len(CANDS)]
                try:
                    if free_slots(school, tc, batch) > 0:
                        log(f"  ⚠ {tc} 出现空位 —— 探针停手，请去跑 grab.py --live")
                        hit = "SLOT_APPEARED"
                        break
                except Exception as exc:  # noqa: BLE001
                    log(f"  ✗ 容量查询失败，停手: {exc}")
                    hit = "CAPACITY_ERR"
                    break
                if args.gap and i:
                    time.sleep(args.gap)
                payload, status, text, dt = school.submit(creds.student_id, batch, tc, "01")
                verdict, msg = G.classify(payload, status, text)
                marks.append({"i": i + 1, "tc": tc[-3:], "t": round(time.time() - t0, 3),
                              "dt": round(dt, 1), "verdict": verdict, "msg": msg[:50]})
                flag = "⚠" if verdict in THROTTLE else " "
                log(f"   {flag} #{i + 1} T+{time.time() - t0:5.3f}s {tc[-3:]} {dt:5.0f}ms "
                    f"{verdict:<12} {msg[:40]}")
                if verdict in THROTTLE:
                    hit = verdict
                    break
            alive, detail = school.probe()
            log(f"  ⇒ 发出 {len(marks)}/{level} 发，"
                f"{'首限流在第 ' + str(len(marks)) + ' 发' if hit in THROTTLE else '未被限流'}"
                f"；会话{'仍在' if alive else '已失效'}")
            results.append({"level": level, "rep": rep + 1, "sent": len(marks), "gap": args.gap,
                            "hit": hit if isinstance(hit, str) else None,
                            "session_alive": alive, "marks": marks})
            time.sleep(1.5)

    log("")
    log("=" * 78)
    log("  汇总（会话是否被限流/作废）")
    log("=" * 78)
    for r in results:
        log(f"  连发 {r['level']} 发 第{r['rep']}轮：发出 {r['sent']}，"
            f"{'⚠ ' + str(r['hit']) if r['hit'] else '✅ 全程正常'}，"
            f"会话{'仍在' if r['session_alive'] else '已失效'}")
    os.makedirs(args.out, exist_ok=True)
    path = Path(args.out) / f"burst_shape_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"  记录: {path}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
