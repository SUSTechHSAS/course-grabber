#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在线真值探测：验证「验证码 + 登录协议」这条链路真的通，且**不碰任何真实账号**。

原理（与 click-captcha-matcher/eval_oracle.py 同一套判定）：
    学校在检查账号之前先检查验证码，所以用**不存在的学号**提交：
        回「登录名或密码不正确」= 验证码通过了（卡在账号这一步）
        回「验证码不正确」        = 这次的验证码没解对
    因此这个脚本可以放心反复跑 —— 不存在的学号既登不进任何账号，也不会被锁。

它走的正是 school_auth 的生产代码路径（抓图 → click-captcha-matcher 识别 → DES+Base64 加密
→ login.do），所以通过就等于 grab.py 的自动登录能通。

    python3 auth_probe.py --n 20                 # 20 轮，每轮之间 1 秒
    python3 auth_probe.py --n 50 --gap 1.5 --out probe/
"""

from __future__ import annotations

import argparse
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

import school_auth as A  # noqa: E402

BOGUS_ID = "2000000000"          # 不存在的学号：结构上不可能登录成功
BOGUS_PWD = "not-a-real-password"


def main() -> int:
    ap = argparse.ArgumentParser(description="登录链路在线真值探测（假学号，零账号风险）")
    ap.add_argument("--n", type=int, default=20, help="探测轮数，默认 20")
    ap.add_argument("--gap", type=float, default=1.0, help="每轮间隔秒数，默认 1.0")
    ap.add_argument("--out", default=None, help="把图与结果存到这个目录（默认不存）")
    ap.add_argument("--click-captcha-matcher-dir", default=None)
    ap.add_argument("--click-captcha-matcher", default=None)
    ap.add_argument("--attempts", type=int, default=1,
                    help="每轮最多换几张验证码，默认 1（要测的是单张准确率）")
    args = ap.parse_args()

    solver = A.CaptchaSolver(args.captcha_model_dir, args.captcha_model)
    print(f"模型: {solver.model}")
    print(f"假学号 {BOGUS_ID}，{args.n} 轮，间隔 {args.gap}s —— 不会碰到任何真实账号\n")
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    creds = A.Credentials(BOGUS_ID, BOGUS_PWD, "probe")
    stats: collections.Counter[str] = collections.Counter()
    passed = images = 0
    t_start = time.time()
    for i in range(1, args.n + 1):
        t0 = time.time()
        login = A.Login(creds, solver, captcha_attempts=args.attempts,
                        gap=0.3, log=lambda *a: None)
        verdict, detail, img = "", "", b""
        try:
            challenge = A.fetch_captcha()
            img = challenge.image
            images += 1
            points, margin = solver.solve(img)
            login._submit(challenge, A.CaptchaSolver.to_verify_code(points))
            verdict, detail = "UNEXPECTED", "假学号竟然登录成功了？"
        except A.BadCredentials as exc:
            verdict, detail = "CAPTCHA_OK", f"验证码通过（卡在账号）margin={margin:.3f}"
            passed += 1
        except A.CaptchaRejected as exc:
            verdict, detail = "CAPTCHA_BAD", f"{exc} margin={margin:.3f}"
        except A.LoginUnavailable as exc:
            verdict, detail = "UNAVAILABLE", str(exc)
        except Exception as exc:  # noqa: BLE001
            verdict, detail = "ERROR", f"{type(exc).__name__}: {exc}"
        stats[verdict] += 1
        acc = passed / max(1, sum(v for k, v in stats.items()
                                  if k in ("CAPTCHA_OK", "CAPTCHA_BAD")))
        print(f"#{i:>3} {'✓' if verdict == 'CAPTCHA_OK' else '✗'} {verdict:<11} "
              f"{detail[:64]:<64} acc={acc:.3f}", flush=True)
        if args.out and img:
            name = f"{int(time.time() * 1000)}_{'ok' if verdict == 'CAPTCHA_OK' else 'bad'}.jpg"
            (Path(args.out) / name).write_bytes(img)
            with open(Path(args.out) / "results.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"file": name, "verdict": verdict, "detail": detail,
                                     "margin": margin if verdict.startswith("CAPTCHA") else None},
                                    ensure_ascii=False) + "\n")
        if verdict in ("UNAVAILABLE", "ERROR") and stats[verdict] >= 3:
            print("连续异常过多，停下")
            break
        time.sleep(max(0.0, args.gap - (time.time() - t0)))

    solved = stats["CAPTCHA_OK"] + stats["CAPTCHA_BAD"]
    print("\n" + "=" * 72)
    print(f"  验证码通过 {passed}/{solved} = {passed / max(1, solved):.4f}"
          f"   共取图 {images} 张   耗时 {time.time() - t_start:.0f}s")
    print(f"  分类统计: {dict(stats)}")
    if solved:
        lo = passed / solved
        print(f"  单张识别率 95% 置信下限 ≈ {lo - 1.96 * (lo * (1 - lo) / solved) ** 0.5:.3f}")
    print("=" * 72)
    return 0 if passed == solved and solved else 1


if __name__ == "__main__":
    raise SystemExit(main())
