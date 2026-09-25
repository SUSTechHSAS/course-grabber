#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发形状探针：同一瞬间打出去的几发写请求，学校真的会当成几发来处理吗？

2026-09-25 用 `--hedge --conns 3` 实测发现：
    3 发**同时**写出去时，只有 1 发拿到正常的业务回复「该课程超过课容量」，
    另外 2 发是 `code=2` 但 **msg 为空** 的怪回复。看起来学校对同一个学生的并发提交
    做了串行化/互斥，输的那两发根本没被真正评估 —— 也就是说 3 发并排打出去
    并不等于 3 次机会，反而把每秒 3 发的额度浪费掉 2 发。

本探针比较三种形状，每种都用**全新会话**：
    parallel  3 发同一瞬间（等价于 --hedge）
    stagger   3 发间隔 --stagger 秒（默认 0.15，仍然在"每秒 3 发"额度内）
    single    每次只打 1 发，间隔 --stagger 秒（对照组）

安全：只用当前满员的真实候选；每发前只读确认仍满员，出现空位立刻停手；
operationType 恒为 "1"，无退选路径。

    python3 concurrent_probe.py --reps 3 --stagger 0.15
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import school_auth as A          # noqa: E402
import grab as G          # noqa: E402

CANDS = [tc for tc, _l, _g in G.DEFAULT_CANDIDATES]
THROTTLE = (G.V_BUSY, G.V_EXPIRED)


def log(msg: str) -> None:
    print(msg, flush=True)


def shape_of(verdict: str, raw: str) -> str:
    """把回复归成"正常业务回复"还是"空 msg 的怪回复"。"""
    if verdict == G.V_FULL:
        return "FULL"
    if "超过课容量" in raw:
        return "FULL"
    if verdict in THROTTLE:
        return "THROTTLE"
    if '"msg":""' in raw or '"msg":null' in raw:
        return "EMPTY_MSG"
    return verdict


def one_round(mode: str, school, creds, batch: str, solver, stagger: float) -> dict:
    school.adopt(A.Login(creds, solver, captcha_attempts=4, log=lambda m: None).login())
    targets = CANDS[:3]
    for tc in targets:                       # 满员自检（只读）
        cap = school.capacity(tc, batch)
        free = (int(cap.get("nonMainClassCapacity") or 0)
                - int(cap.get("nonMainElectiveNumber") or 0))
        if free > 0:
            return {"mode": mode, "abort": f"{tc} 出现空位", "marks": []}

    marks: list[dict] = []
    lock = threading.Lock()
    t0 = time.time()

    def fire(tc: str, delay: float) -> None:
        time.sleep(delay)
        try:
            payload, status, text, dt = school.submit(creds.student_id, batch, tc, "01")
            verdict, _msg = G.classify(payload, status, text)
        except Exception as exc:  # noqa: BLE001
            verdict, text, dt = "EXC", str(exc), 0.0
        with lock:
            marks.append({"tc": tc[-3:], "t": round(time.time() - t0, 3),
                          "shape": shape_of(verdict, text), "verdict": verdict,
                          "raw": text[:90]})

    if mode in ("parallel", "stagger"):
        # 走 sniper：每发一条独立裸 TCP 连接，与 --hedge 首发完全同一条代码路径。
        # （用 school.submit 的"并发"是假的：那把连接锁会把它们排成队。）
        out: list = []
        base = time.time() + 0.4
        gap = 0.0 if mode == "parallel" else stagger
        plan = [(G.build_wire(school, creds.student_id, batch, tc, "01"), tc)
                for tc in targets]
        threads = [threading.Thread(target=G.sniper,
                                    args=(wire, tc, base + i * gap, G.State(), out, i, None))
                   for i, (wire, tc) in enumerate(plan)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
        for idx, tc, status, body, dt in sorted(out):
            text = body.decode("utf-8", "replace")
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {}
            verdict, _msg = G.classify(payload, status, text)
            marks.append({"tc": tc[-3:], "t": round(time.time() - t0 - 0.4, 3),
                          "shape": shape_of(verdict, text), "verdict": verdict,
                          "raw": text[:90]})
    else:
        gap = stagger * 2
        threads = [threading.Thread(target=fire, args=(tc, i * gap))
                   for i, tc in enumerate(targets)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    alive, _d = school.probe()
    shapes = collections.Counter(m["shape"] for m in marks)
    log(f"    时刻 {[m['t'] for m in marks]}  形状 {dict(shapes)}  "
        f"会话{'仍在' if alive else '已失效'}")
    for m in sorted(marks, key=lambda x: x["t"]):
        log(f"      {m['tc']} T+{m['t']:.3f}s {m['shape']:<10} {m['raw'][:60]}")
    return {"mode": mode, "marks": marks, "shapes": dict(shapes), "alive": alive}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--stagger", type=float, default=0.15)
    ap.add_argument("--modes", default="parallel,stagger,single")
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
    log("  并发形状探针：3 发同时 vs 错开 vs 单发（真实满员候选）")
    log("=" * 78)

    results = []
    for mode in [m for m in args.modes.split(",") if m.strip()]:
        for rep in range(1, args.reps + 1):
            log("")
            log(f"── {mode} 第 {rep}/{args.reps} 轮 ──")
            school.adopt(A.Login(creds, solver, captcha_attempts=4,
                                 log=lambda m: None).login())
            d = school.student(creds.student_id).get("data") or {}
            batch = str((d.get("electiveBatch") or {}).get("code") or "")
            r = one_round(mode, school, creds, batch, solver, args.stagger)
            results.append(r)
            time.sleep(2.0)

    log("")
    log("=" * 78)
    log("  汇总：3 发里拿到「正常业务回复」的个数")
    log("=" * 78)
    agg: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in results:
        if r.get("abort"):
            continue
        for s, n in r["shapes"].items():
            agg[r["mode"]][s] += n
    for mode, c in agg.items():
        total = sum(c.values())
        log(f"  {mode:<9} 共 {total} 发：{dict(c)}   "
            f"真实评估率 {c['FULL'] / max(total, 1):.0%}")
    os.makedirs(args.out, exist_ok=True)
    path = Path(args.out) / f"concurrent_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"  记录: {path}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
