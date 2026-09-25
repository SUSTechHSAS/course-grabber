#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""过载注入测试：放课瞬间服务器被打爆时，抢课循环能打成什么样。

真实 20:00 的服务器症状（用户实测反馈 + 09-24 日志）：
    * 头几秒请求根本回不来（连接挂住 / 超时）
    * 网关直接 502/503/504 或返回 HTML 错误页
    * 学校回「选课系统正在初始化，请稍候」
    * 连接被重置

这个脚本起一个**假学校**，按剧本制造上述症状，然后驱动 grab 里**真正的**
`_retry_loop`，量三个指标：
    ① 窗口内一共打出去几发写请求
    ② 服务器恢复后多久拿到确认成功
    ③ 各种畸形回复被归类成了什么

它不联网、不碰真实账号，纯粹测客户端在恶劣网络下的行为。

    python3 overload_probe.py --scenario hang --window 20
    python3 overload_probe.py --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import grab as G  # noqa: E402

TARGET = "000000000000000000000001"
OTHERS = ["000000000000000000000002", "000000000000000000000003"]
BATCH = "BATCH1"


class FakeSchool:
    """会按剧本过载的假学校。"""

    def __init__(self, scenario: str, bad_seconds: float) -> None:
        self.scenario = scenario
        self.bad_until = time.time() + bad_seconds
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.writes: list[dict] = []          # 每一次 volunteer.do 的记录
        self.submitted = False                # 恢复后是否已"选上"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _overloaded(self) -> bool:
                return time.time() < outer.bad_until

            def _bad_reply(self) -> None:
                """按剧本给出过载期的畸形回复。"""
                sc = outer.scenario
                if sc == "hang":
                    time.sleep(30)            # 挂住不回，测客户端超时
                elif sc == "slow":
                    time.sleep(6)
                elif sc == "gateway502":
                    self._send(502, b"<html><body>502 Bad Gateway</body></html>", "text/html")
                elif sc == "reset":
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                elif sc == "init":
                    self._send(200, json.dumps(
                        {"code": "0", "msg": "选课系统正在初始化,请稍候..."}).encode())
                else:
                    self._send(200, json.dumps({"code": "0", "msg": ""}).encode())

            def do_POST(self):                # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace")
                path = self.path.split("?")[0]
                if path.endswith("volunteer.do"):
                    form = dict(urllib.parse.parse_qsl(raw))
                    add = json.loads(form.get("addParam") or "{}").get("data", {})
                    tc = str(add.get("teachingClassId") or "")
                    with outer.lock:
                        outer.writes.append({"t": round(time.time() - outer.t0, 3), "tc": tc[-3:]})
                    if self._overloaded():
                        self._bad_reply()
                        return
                    with outer.lock:
                        outer.submitted = True
                    self._send(200, json.dumps(
                        {"code": "1", "msg": "添加选课志愿成功"}).encode())
                    return
                if path.endswith("capacity.do"):
                    self._send(200, json.dumps({"code": "1", "data": {
                        "nonMainClassCapacity": "2", "nonMainElectiveNumber": "0"}}).encode())
                    return
                if path.endswith("studentstatus.do"):
                    self._send(200, json.dumps({"code": "1"}).encode())
                    return
                if "courseResult" in path:
                    rows = [{"teachingClassID": TARGET}] if outer.submitted else []
                    self._send(200, json.dumps({"code": "1", "dataList": rows}).encode())
                    return
                if "/student/" in path:
                    self._send(200, json.dumps({"code": "1", "data": {
                        "campus": "01", "electiveBatch": {"code": BATCH}}}).encode())
                    return
                self._send(200, b"{}")

            do_GET = do_POST

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def run(scenario: str, bad_seconds: float, window: float) -> dict:
    fake = FakeSchool(scenario, bad_seconds)
    old_host, old_port = G.HOST, G.PORT
    G.HOST, G.PORT = "127.0.0.1", fake.port
    logs: list[str] = []
    try:
        school = G.School("tok", "JSESSIONID=x; _WEU=y", "http://x/grablessons.do?token=tok")
        school.code = "2026000000"
        school.pacer = G.WritePacer(per_window=3, window=1.0, margin=0.15, min_gap=0.10)
        args = G.parse_args(["--url", "http://x/y.do?token=t", "--live",
                             "--window", str(window), "--burst", str(window),
                             "--interval", "1.0", "--slow", "1.0"])
        args.stagger = 0.10
        groups = [("星期三-3-5", [(TARGET, "目标班"), (OTHERS[0], "备选A"), (OTHERS[1], "备选B")])]
        state = G.State()
        t0 = time.time()
        G._retry_loop(school, "2026000000", BATCH, "01", groups, state, args, t0)
        elapsed = time.time() - t0
        ok_at = None
        if state.confirmed:
            # 以"写请求发出时刻"为准估算：确认发生在成功那一发之后
            ok_at = round(elapsed, 2)
        return {"scenario": scenario, "bad_seconds": bad_seconds, "window": window,
                "writes": len(fake.writes), "confirmed": state.confirmed,
                "confirmed_after": ok_at, "elapsed": round(elapsed, 2),
                "write_times": [w["t"] for w in fake.writes][:12],
                "logs": logs}
    finally:
        G.HOST, G.PORT = old_host, old_port
        fake.close()


SCENARIOS = [
    ("hang", 3.0, "头 3 秒完全挂住不回（最像 20:00 真实症状）"),
    ("gateway502", 3.0, "头 3 秒网关 502 + HTML 错误页"),
    ("init", 3.0, "头 3 秒回「选课系统正在初始化」"),
    ("reset", 3.0, "头 3 秒连接被重置"),
    ("slow", 3.0, "头 3 秒每次响应慢 6 秒"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--window", type=float, default=20.0)
    ap.add_argument("--bad-seconds", type=float, default=3.0)
    args = ap.parse_args()

    todo = SCENARIOS if (args.all or not args.scenario) else [
        s for s in SCENARIOS if s[0] == args.scenario]
    if not todo:
        print(f"未知剧本 {args.scenario}，可选: {[s[0] for s in SCENARIOS]}")
        return 2

    print("=" * 78)
    print("  过载注入测试（假学校，驱动真实的 _retry_loop，不联网、不碰账号）")
    print("=" * 78)
    results = []
    for name, bad, desc in todo:
        print(f"\n── {name}: {desc}（{bad:.0f}s，窗口 {args.window:.0f}s）──")
        r = run(name, bad, args.window)
        results.append(r)
        print(f"  写请求 {r['writes']} 发  首发时刻 {r['write_times'][:5]}")
        print(f"  结果: {'✅ 确认选中' if r['confirmed'] else '❌ 窗口内没成功'}"
              f"  确认耗时 {r['confirmed_after'] or '-'}s  总耗时 {r['elapsed']}s")

    print("\n" + "=" * 78)
    print("  汇总")
    print("=" * 78)
    print(f"  {'剧本':<12} {'写请求':>6} {'成功':>5} {'成功后耗时':>10} {'窗口':>6}")
    for r in results:
        print(f"  {r['scenario']:<12} {r['writes']:>6} "
              f"{'✅' if r['confirmed'] else '❌':>5} "
              f"{str(r['confirmed_after'] or '-'):>10} {r['window']:>5.0f}s")
    out = Path("rate-lab")
    out.mkdir(exist_ok=True)
    path = out / f"overload_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  记录: {path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
