#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检 —— 不联网、不需要密码、不需要真实账号。

    1. 密码加密与前端 JS 的历史输出（golden vectors）逐字符一致
    2. 验证码模型能加载，并对合成图给出合法的 4 个点击坐标
    3. 凭据文件解析 / 权限告警 / 命令行覆盖
    4. 会话字段拼装（token + cookie → Referer / token 头）
    5. 用本地假学校服务端跑通：登录协议、重登录策略、只读自愈、写请求绝不重放

    用的是 tests/config.test.json 这份假配置，所以不需要任何真实的学校信息。

    python3 tests/test_offline.py
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import urllib.parse
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# 必须赶在 import 业务模块之前指好配置 —— school_config 是在导入时读配置的
os.environ.setdefault("COURSE_GRABBER_CONFIG", str(HERE / "config.test.json"))
sys.path.insert(0, str(ROOT))

import school_auth as A  # noqa: E402

PASS = 0
FAIL = 0

# 一张"够用"的假 JPEG：mock 服务端只校验魔数，桩 solver 不看内容。
# 这样整套离线自检**不需要 PIL、不需要 onnxruntime、不需要模型权重**就能跑。
FAKE_JPEG = (b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")


class StubSolver:
    """没有真模型时的替身：固定返回 4 个合法坐标，足以跑通协议与策略测试。"""

    min_margin = 0.0

    def solve(self, image):
        return [[10, 10], [40, 10], [70, 10], [100, 10]], 0.5

    @staticmethod
    def to_verify_code(points):
        return ",".join(f"{x}-{y}" for x, y in points)


def get_solver():
    """有模型就用真模型，没有就退回桩 —— 两种情况都返回可用的 solver。"""
    try:
        return A.CaptchaSolver(), True
    except Exception:  # noqa: BLE001 - 缺模型/缺 onnxruntime 都算"没有"
        return StubSolver(), False


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += ok
    FAIL += not ok
    print(f"  {'✓' if ok else '✗'} {name}{'  ' + detail if detail else ''}")


# --------------------------------------------------------------------------
def test_password_vectors() -> None:
    """DES 那一段是整个登录里唯一"错了也看不出来"的地方，用固定向量锁住。

    向量由另一份独立实现（前端 JS）生成，只留输入输出，不含任何环境信息。
    """
    print("\n[1] 密码加密（golden vectors）")
    from cus_base64 import CustomBase64
    from desencode import str_enc

    keys = ("this", "password", "is")            # 与向量生成时一致
    vectors = [
    ('abc', 'N0QyMEFBM0M2ODQ0MTdGRg=='),
    ('abcd', 'MkVCNURGQUY0NUI4MzdFNA=='),
    ('abcde', 'MkVCNURGQUY0NUI4MzdFNDA5RjlGQkY1QzI3NUE5MUY='),
    ('abcdefgh', 'MkVCNURGQUY0NUI4MzdFNDNBMEQ0ODY2NjA5NEQyRDg='),
    ('0123456789abcdef', 'MTlBOUE2OTk5NDI4Mjg1RUQwNzUyMjU4RkFFQjZGRkNCRTk4Qjk4M0Y3QTVDQzMxMTVDQ0IzQTNDNEE0MEI3RQ=='),
    ('P@ssw0rd!', 'NkQ0MEQ5MUMwN0IwRjJFQTkxNkQzRUVFMzAwMERFNTg4MDlCQzU2QjU3Q0Y5QzMx'),
    ('xxxxxxxxxxxxxxx', 'Q0QzMTkwMzU3QzYzNDhDRUNEMzE5MDM1N0M2MzQ4Q0VDRDMxOTAzNTdDNjM0OENFNzZBQzQxNDRFMEVEMkJBMw=='),
    ('短密码', 'Qzk5RjAwNzk3QzE4RDUzRg=='),
    ('混合Mixed123', 'RjQyMUM4MjQwOUExRDZGNTJENzREMkUxOTRGQkQ4NjA5NzFBRTkyMjY3Q0JFMjZB'),
    ]
    b64 = CustomBase64()
    bad = [(p, w, b64.encode(str_enc(p, *keys))) for p, w in vectors
           if b64.encode(str_enc(p, *keys)) != w]
    check(f"{len(vectors)} 组向量完全一致", not bad, str(bad[:2]))
    check("encrypt_password 用的是配置里的密钥",
          A.encrypt_password("abc") == b64.encode(str_enc("abc", *A.CFG.des_keys)))


# --------------------------------------------------------------------------
def test_captcha_solver() -> None:
    """验证码模型（可选依赖）：能加载、能对合成图给出 4 个合法坐标。"""
    print("\n[2] 验证码模型（可选）")
    model_dir = A.DEFAULT_MODEL_DIR
    if not (model_dir / "solver.py").exists():
        print(f"  – 跳过（没找到识别项目 {model_dir}；把它 clone 到同级目录即可启用）")
        return
    solver, real = get_solver()
    if not real:
        print(f"  – 跳过（模型或 onnxruntime 不可用：{model_dir}）")
        return
    check("加载模型", True, str(getattr(solver, "model", "?")))

    # 用姊妹仓库的 synth.py 现场合成一批图（不依赖任何真实样本）
    sys.path.insert(0, str(model_dir))
    try:
        import synth
    except Exception as exc:  # noqa: BLE001
        print(f"  – 跳过（合成器不可用: {exc}）")
        return
    rng = __import__("random").Random(7)
    ok = 0
    margins = []
    for _ in range(10):
        img = synth.make(rng) if hasattr(synth, "make") else None
        if img is None:
            print("  – 跳过（synth 接口不认识）")
            return
        blob = img if isinstance(img, bytes) else img[0]
        points, margin = solver.solve(blob)
        sane = (len(points) == 4 and len({tuple(p) for p in points}) == 4
                and all(0 <= x <= A.CAPTCHA_WIDTH and 0 <= y <= A.CAPTCHA_HEIGHT
                        for x, y in points))
        ok += sane
        margins.append(margin)
    check(f"{ok}/10 张合成图给出合法坐标", ok == 10,
          f"最低 margin={min(margins):.3f}")


# --------------------------------------------------------------------------
def test_credentials() -> None:
    print("\n[3] 凭据文件")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "credentials.json"
        path.write_text(json.dumps({"student_id": "2026000000", "password": "s3cret"}),
                        encoding="utf-8")
        os.chmod(path, 0o600)
        creds = A.load_credentials(path)
        check("标准字段", creds is not None and creds.student_id == "2026000000"
              and creds.password == "s3cret")

        path.write_text(json.dumps({"credentials": {"studentId": 2026000000, "pwd": "p"}}),
                        encoding="utf-8")
        creds = A.load_credentials(path)
        check("别名 + 少一层包装", creds is not None and creds.student_id == "2026000000")

        path.write_text(json.dumps({"student_id": "2026000000"}), encoding="utf-8")
        try:
            A.load_credentials(path)
            check("缺密码要报错", False)
        except A.AuthError:
            check("缺密码要报错", True)

        check("没有文件返回 None", A.load_credentials(Path(td) / "nope.json") is None)
        creds = A.load_credentials(path, student_id="2026000000", password="cli")
        check("命令行覆盖文件", creds.password == "cli" and creds.source == "命令行")
        check("不泄漏密码到 repr", "s3cret" not in repr(A.Credentials("1", "s3cret", "x")))


# --------------------------------------------------------------------------
def test_session_shape() -> None:
    print("\n[4] 会话拼装")
    s = A.LoginSession(token="abc-123", cookie="JSESSIONID=X; route=Z; insert_cookie=W")
    check("Referer 带新 token", s.referer().endswith("token=abc-123"))
    check("Cookie 含配置里要求的名字",
          all(k in s.cookie for k in (*A.CFG.session_cookies, *A.CFG.captcha_cookies)))
    check("坐标串格式 x-y,x-y,...",
          A.CaptchaSolver.to_verify_code([[1, 2], [3, 4], [5, 6], [7, 8]])
          == "1-2,3-4,5-6,7-8")
    check("Set-Cookie 解析不被 Expires 逗号带偏",
          A._set_cookie_values(["route=a; Path=/, extra=b; Expires=Wed, 21 Oct 2026 07:28:00 GMT"],
                               ("extra", "route")) == "extra=b; route=a")


# --------------------------------------------------------------------------
class _MockSchool:
    """一个只说登录协议那三句话的假学校（线程内 HTTP 服务）。"""

    def __init__(self, image: bytes, password: str, script: list[dict],
                 guard_session: bool = False) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.image = image
        self.want_pwd = A.encrypt_password(password)
        self.script = list(script)          # 每次 login.do 消费一条：{"code": "1"/"2"/"3"/"4"}
        self.guard_session = guard_session  # True: 业务接口要求 JSESSIONID=S1，否则算未登录
        self.calls: list[tuple[str, dict, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):      # 安静
                pass

            def _send(self, body: bytes, ctype: str, cookies: list[str] = ()) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.end_headers()
                self.wfile.write(body)

            def _expired(self) -> bool:
                return outer.guard_session and "JSESSIONID=S1" not in (
                    self.headers.get("Cookie") or "")

            def do_POST(self):              # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8")
                form = dict(urllib.parse.parse_qsl(raw))
                outer.calls.append((self.path.split("?")[0], form,
                                    self.headers.get("Cookie") or ""))
                if self.path.startswith(A.PATH_VCODE):
                    self._send(json.dumps({"code": "1", "data": {"token": "vt-1"}}).encode(),
                               "application/json")
                elif self.path.startswith(A.PATH_LOGIN):
                    step = outer.script.pop(0) if outer.script else {"code": "1"}
                    code = str(step.get("code", "1"))
                    if code == "1":
                        body = {"code": "1", "data": {"token": "tok-new", "name": "测试同学",
                                                      "number": form.get("loginName")}}
                        self._send(json.dumps(body).encode(), "application/json",
                                   ["JSESSIONID=S1; Path=/", "EXTRA=W1; Path=/"])
                    else:
                        self._send(json.dumps({"code": code,
                                               "msg": step.get("msg", "")}).encode(),
                                   "application/json")
                elif self._expired():
                    self._send(json.dumps({"code": "302", "msg": "未登录用户"}).encode(),
                               "application/json")
                else:
                    self._send(json.dumps({"code": "1", "data": {"campus": "01"},
                                           "dataList": []}).encode(), "application/json")

            def do_GET(self):               # noqa: N802
                outer.calls.append((self.path.split("?")[0], {},
                                    self.headers.get("Cookie") or ""))
                if self.path.startswith(A.PATH_VCIMAGE):
                    self._send(outer.image, "image/jpeg",
                               ["route=r1; Path=/", "insert_cookie=ic1; Path=/"])
                else:
                    self._send(b"", "text/plain")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _with_mock(script: list[dict], fn, *, guard_session: bool = False) -> None:
    """把 school_auth / grab 指向本地假学校跑一段逻辑，跑完恢复常量。"""
    mock = _MockSchool(FAKE_JPEG, "pwd-123", script, guard_session=guard_session)
    old = (A.HOST, A.PORT)
    A.HOST, A.PORT = "127.0.0.1", mock.port
    try:
        fn(mock)
    finally:
        A.HOST, A.PORT = old
        mock.close()


def test_login_flow_mock() -> None:
    print("\n[5] 假学校服务端：登录协议 + 重登录策略")
    solver, _real = get_solver()
    creds = A.Credentials("2026000000", "pwd-123", "test")

    # a) 一次通过
    def happy(mock: _MockSchool) -> None:
        session = A.Login(creds, solver, gap=0.01, log=lambda *a: None).login()
        check("登录成功拿到 token", session.token == "tok-new", session.cookie)
        check("Cookie 含配置里要求的名字",
              all(k in session.cookie
                  for k in (*A.CFG.session_cookies, *A.CFG.captcha_cookies)))
        login_call = [c for c in mock.calls if c[0] == A.PATH_LOGIN][0]
        check("loginPwd 是学校协议的密文", login_call[1]["loginPwd"] == mock.want_pwd)
        check("loginName/vtoken 传对",
              login_call[1]["loginName"] == "2026000000" and login_call[1]["vtoken"] == "vt-1")
        check("verifyCode 是 4 组 x-y",
              len(login_call[1]["verifyCode"].split(",")) == 4
              and all("-" in p for p in login_call[1]["verifyCode"].split(",")))
        check("登录请求只带本轮验证码 Cookie（绝不带旧会话）",
              "JSESSIONID" not in login_call[2] and "route=r1" in login_call[2])

    _with_mock([], happy)

    # b) 第一次验证码被拒 → 换一张图再来
    def retry_captcha(mock: _MockSchool) -> None:
        log: list[str] = []
        session = A.Login(creds, solver, gap=0.01, log=log.append).login()
        images = len([c for c in mock.calls if c[0] == A.PATH_VCIMAGE])
        check("验证码被拒后自动换图", session.token == "tok-new" and images == 2,
              f"{images} 张图")

    _with_mock([{"code": "3", "msg": "验证码不正确"}, {"code": "1"}], retry_captcha)

    # c) 密码错 → 立刻熔断，绝不再试
    def wrong_password(mock: _MockSchool) -> None:
        mgr = A.ReloginManager(creds, solver, captcha_attempts=3, min_gap=0.01,
                               cooldown=0, log=lambda *a: None)
        check("密码错返回 None", mgr.relogin("t") is None)
        check("密码错后熔断（不再发请求）", mgr.fatal is not None and not mgr.available)
        before = len(mock.calls)
        check("第二次调用直接拒绝", mgr.relogin("t") is None and len(mock.calls) == before)
        check("熔断原因说清是凭据问题", "密码" in (mgr.fatal or ""))

    _with_mock([{"code": "2", "msg": "登录名或密码不正确"}], wrong_password)

    # d) 在线人数超限 → 可重试，但受次数上限约束
    def online_limit(mock: _MockSchool) -> None:
        mgr = A.ReloginManager(creds, solver, max_logins=2, captcha_attempts=1,
                               min_gap=0.01, cooldown=0, log=lambda *a: None)
        check("超限第 1 次失败", mgr.relogin("t") is None and mgr.available)
        check("超限第 2 次失败后到上限", mgr.relogin("t") is None and not mgr.available)
        before = len(mock.calls)              # 每轮 = vcode.do + image.do + login.do
        check("上限后不再发任何请求", mgr.relogin("t") is None and len(mock.calls) == before,
              f"共 {before} 次调用")

    _with_mock([{"code": "4", "msg": "在线人数超过上限"},
                {"code": "4", "msg": "在线人数超过上限"}], online_limit)


def test_school_wiring() -> None:
    """验证 grab.School 里的接线：只读会自动救、写接口绝不自动重放。"""
    print("\n[6] grab.School 接线（假学校）")
    import grab as G

    mock = _MockSchool(FAKE_JPEG, "pwd-123", [], guard_session=True)
    old_a, old_g = (A.HOST, A.PORT), (G.HOST, G.PORT)
    A.HOST, A.PORT = "127.0.0.1", mock.port
    G.HOST, G.PORT = "127.0.0.1", mock.port
    logs: list[str] = []
    try:
        creds = A.Credentials("2026000000", "pwd-123", "test")
        solver, _real = get_solver()

        # a) 只读接口 → 会话过期 → 自动重登录 → 重放成功
        school = G.School("stale-token", "JSESSIONID=OLD", "http://x/grablessons.do?token=stale-token")
        school.code = "2026000000"
        school.auth = A.ReloginManager(creds, solver, min_gap=0.01, cooldown=0,
                                      captcha_attempts=2, log=logs.append)
        payload = school.student("2026000000")
        check("只读接口过期后自动恢复", str(payload.get("code")) == "1", str(payload)[:60])
        check("token/Cookie 已换成新会话",
              school.token == "tok-new"
              and f"{A.CFG.session_cookies[0]}=S1" in school.cookie)
        check("Referer 跟着换成新 token", "tok-new" in school.referer)
        check("relogins 计数 = 1", school.relogins == 1)
        check("日志里说明是自动重登录", any("自动重登录" in x for x in logs))

        # b) 写接口（recover=False）→ 绝不自动重放，也不偷偷登录
        school2 = G.School("stale-token", "JSESSIONID=OLD", "http://x/grablessons.do?token=stale")
        school2.code = "2026000000"
        school2.auth = A.ReloginManager(creds, solver, min_gap=0.01, cooldown=0, log=lambda *a: None)
        before = len([c for c in mock.calls if c[0] == A.PATH_LOGIN])
        payload, _s, _t, _ms = school2.submit("2026000000", "B1", "TC1", "01")
        after = len([c for c in mock.calls if c[0] == A.PATH_LOGIN])
        verdict, _msg = G.classify(payload, 200, json.dumps(payload))
        check("写接口被判为 EXPIRED", verdict == G.V_EXPIRED, verdict)
        check("写接口没有触发自动登录", after == before, f"{before} → {after}")
        check("写接口没有被重放", len([c for c in mock.calls
                                      if c[0] == G.PATH_VOLUNTEER]) == 1)
        check("会话仍是旧的（没被偷偷换掉）", school2.token == "stale-token")
    finally:
        A.HOST, A.PORT = old_a
        G.HOST, G.PORT = old_g
        mock.close()


def main() -> int:
    print("=" * 72)
    print("  离线自检（不联网、不需要真实账号）")
    print("=" * 72)
    test_password_vectors()
    test_captcha_solver()
    test_credentials()
    test_session_shape()
    test_login_flow_mock()
    test_school_wiring()
    print("\n" + "=" * 72)
    print(f"  通过 {PASS}   失败 {FAIL}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
