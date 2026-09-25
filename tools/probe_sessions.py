#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话探针：同一时间能存在几个有效会话？多会话能不能把写额度翻倍？

要回答两件事：
    1. 新登录一次，旧会话还活着吗？（决定"脚本登录会不会踢掉我 Chrome 的会话"）
    2. 写接口"每秒 3 发"的额度是按**会话**算还是按**账号**算？
       如果是按会话，那么开 2~3 个会话就能把放课瞬间的出手次数翻倍。

安全：
    * 写请求只用当前满员的真实候选，每发前只读确认仍满员，出现空位立刻停手；
    * operationType 恒为 "1"（本文件不存在退选路径）；
    * 只读部分用 student.do，不改变任何状态。

    python3 session_probe.py
"""

from __future__ import annotations

import collections
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

CANDS = [tc for tc, _l, _g in G.DEFAULT_CANDIDATES]
THROTTLE = (G.V_BUSY, G.V_EXPIRED)


def log(msg: str = "") -> None:
    print(msg, flush=True)


def new_session(creds, solver, label: str) -> tuple[G.School, object]:
    t0 = time.time()
    sess = A.Login(creds, solver, captcha_attempts=4, log=lambda m: None).login()
    school = G.School(sess.token, sess.cookie, sess.referer())
    school.code = creds.student_id
    school.write_timeout = 4.0
    log(f"  登录 {label}: token {sess.token[:8]}…{sess.token[-4:]}  ({time.time() - t0:.1f}s)")
    return school, sess


def probe(school: G.School, code: str) -> tuple[bool, str]:
    ok, payload = school.probe()
    return ok, str(payload.get("msg") or payload.get("code") or "")


def main() -> int:
    # 这些探针都会用真实账号登录 —— 学校同一账号只允许一个有效会话，
    # 真跑着 grab 的时候再跑探针会把它的会话顶掉。
    if not G.acquire_instance_lock():
        return 2
    creds = A.load_credentials()
    if creds is None:
        log("✗ 需要凭据文件")
        return 2
    solver = A.CaptchaSolver()

    log("=" * 78)
    log("  一、会话共存：连续登录 3 次，回头看每一次的会话还活不活")
    log("=" * 78)
    sessions: list[tuple[str, G.School]] = []
    history: list[str] = []
    for label in ("A", "B", "C"):
        school, _sess = new_session(creds, solver, label)
        sessions.append((label, school))
        row = []
        for other, s2 in sessions:
            ok, _msg = probe(s2, creds.student_id)
            row.append(f"{other}={'活' if ok else '死'}")
        line = f"登录 {label} 之后: " + " ".join(row)
        log(f"    → {line}")
        history.append(line)

    log("")
    log("=" * 78)
    log("  二、被限流踢死之后，重登录能不能立刻把 3 发/秒 的额度刷回来")
    log("=" * 78)
    sa, _ = new_session(creds, solver, "甲")
    d = sa.student(creds.student_id).get("data") or {}
    batch = str((d.get("electiveBatch") or {}).get("code") or "")
    if not batch:
        log("✗ 拿不到批次码")
        return 3

    def full(school: G.School, tc: str) -> bool:
        cap = school.capacity(tc, batch)
        free = (int(cap.get("nonMainClassCapacity") or 0)
                - int(cap.get("nonMainElectiveNumber") or 0))
        return free <= 0

    seq: list[dict] = []
    t0 = time.time()

    def write(school: G.School, tag: str, tc: str) -> str:
        """跳过节拍器直接发（这里就是要测学校那边的额度，不是测我们自己的节拍器）。"""
        school.pacer = None
        payload, status, text, dt = school.submit(creds.student_id, batch, tc, "01", timeout=4.0)
        verdict, _msg = G.classify(payload, status, text)
        shape = ("THROTTLE" if verdict in THROTTLE
                 else "EMPTY" if '"msg":""' in text or '"msg":null' in text
                 else verdict)
        seq.append({"tag": tag, "tc": tc[-3:], "t": round(time.time() - t0, 3),
                    "shape": shape, "raw": text[:70]})
        log(f"    {tag} {tc[-3:]} T+{time.time() - t0:5.3f}s {dt:5.0f}ms {shape:<10} {text[:52]}")
        return shape

    log("  甲：正常 3 发（0.12s 间隔）→ 第 4 发（同一秒内，预期被限流并作废会话）")
    for i, tc in enumerate(CANDS[:3]):
        if not full(sa, tc):
            log(f"    ⚠ {tc} 出现空位 —— 停手")
            return 0
        if i:
            time.sleep(0.12)
        write(sa, "甲", tc)
    if not full(sa, CANDS[0]):
        log("    ⚠ 出现空位 —— 停手")
        return 0
    write(sa, "甲", CANDS[3])                     # 第 4 发：预期 BUSY
    ok_a, _ = probe(sa, creds.student_id)
    log(f"    → 甲会话现在 {'✅仍在' if ok_a else '❌已失效'}")

    log("  立刻重新登录（这会把甲彻底顶掉），拿到乙之后马上再打 3 发")
    sb, _ = new_session(creds, solver, "乙")
    for i, tc in enumerate(CANDS[:3]):
        if not full(sb, tc):
            log(f"    ⚠ {tc} 出现空位 —— 停手")
            return 0
        if i:
            time.sleep(0.12)
        write(sb, "乙", tc)
    ok_b, _ = probe(sb, creds.student_id)

    log("")
    log(f"  甲会话 {'✅仍在' if ok_a else '❌已失效'}   乙会话 {'✅仍在' if ok_b else '❌已失效'}")
    shapes = collections.Counter(m["shape"] for m in seq)
    log(f"  形状统计: {dict(shapes)}")
    log("")
    log("=" * 78)
    log("  结论")
    log("=" * 78)
    log("  1) 连续登录后各会话状态（每一步当时的复查结果）:")
    for line in history:
        log(f"       {line}")
    log("     ⇒ 新登录会立刻作废上一个会话：同一账号同时只有一个有效会话")
    first3 = [m["shape"] for m in seq if m["tag"] == "甲"][:3]
    fourth = [m["shape"] for m in seq if m["tag"] == "甲"][3:4]
    second3 = [m["shape"] for m in seq if m["tag"] == "乙"][:3]
    log(f"  2) 甲前 3 发: {first3}；甲第 4 发: {fourth}；"
         f"重登录后乙 3 发: {second3}")
    log(f"     ⇒ 重登录{'确实' if second3 == ['FULL'] * 3 else '没能'}立刻把额度刷回来")
    out = Path("rate-lab")
    out.mkdir(exist_ok=True)
    path = out / f"session_probe_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": seq, "shapes": dict(shapes),
                             "coexist_history": history}, ensure_ascii=False) + "\n")
    log(f"  记录: {path}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
