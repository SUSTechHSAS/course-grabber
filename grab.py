#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教务系统抢课 · 放课窗口精准首发

对接的是常见的"志愿制"选课系统：提交志愿 → 服务端异步处理 → 已选课程列表是唯一真值。
所有学校相关的地址、路径、Cookie 名、密码加密密钥都在 config.json 里（见 school_config.py），
本文件只负责逻辑。

用到的接口（名字是逻辑名，实际路径在 config.json 的 paths 里配置）:
    volunteer   POST  提交选课志愿          body: addParam={"data":{...}}   header: token
    capacity    POST  查教学班余量
    result      POST  已选课程（唯一真值）
    status      POST  异步处理状态
    sysparam    POST  服务器毫秒时间戳
    program     POST  课程目录（用来建候选白名单）
    student     POST  学生信息（批次、校区、学分上下限）

安全设计（硬约束，写在代码里）:
    1. 本文件不存在任何退选/删除志愿的代码路径，全文不含 operationType":"2"。
       没有任何函数能移除已选课程。
    2. 默认只做只读预检 + 计时演练；必须显式传 --live 才会调用选课提交接口。
    3. 预检发现目标教学班已在"已选课程"里 -> 立即退出，不做任何提交。
    4. 只对预检阶段从课程目录解析出的候选教学班白名单提交，绝不提交白名单以外的。
    5. 首发默认打组内前 3 个候选、彼此错开（--single 可退回只打第一个）；
       所有写请求共用同一份"每滚动窗口 N 发"的额度（WritePacer）。
    6. 自动登录只用本机凭据文件里的学号+密码；一旦服务端回"登录名或密码不正确"，
       立刻熔断整条自动登录链路，绝不用错误密码反复试探（避免账号被锁）。

自动登录 / 被踢重登录（见 school_auth.py）:
    ~/.config/course-grabber/credentials.json   权限 0600
        {"student_id": "你的学号", "password": "你的密码"}
    有了它就不必依赖浏览器：启动时自己登录拿会话；运行中被限流踢掉时自动重新登录、
    把抢课继续下去。验证码交给 click-captcha-matcher（另一个仓库）识别。
    --no-relogin 可以完全关掉这条链路。

用法:
    # 0) 先准备配置（一次性）
    cp config.example.json config.json    # 填上你学校的域名与接口路径

    # 1) 自检（离线，不联网、不需要密码）
    python3 tests/test_offline.py

    # 2) 演练（只读，不提交；验证登录/时钟/候选是否都正常）
    python3 grab.py

    # 3) 实战：19:50 启动，脚本自己等到 20:00:00.000 首发
    python3 grab.py --live

    # 4) 自定义时间 / 重排优先级 / 临时给凭据
    python3 grab.py --live --at 20:00:00
    python3 grab.py --live --priority 0001,0002
    python3 grab.py --live --student 2026xxxxxx --password '...'
"""

from __future__ import annotations

import argparse
import atexit
import collections
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import school_config
except ImportError as _exc:                      # pragma: no cover
    print(f"✗ 缺少 school_config.py: {_exc}")
    raise SystemExit(2)

try:
    _CFG = school_config.load()
    import school_auth
except school_config.ConfigError as _exc:
    # 第一次运行（还没填配置）走这里：给一句人话，不要甩 import 期的 traceback
    print(f"\n✗ {_exc}\n")
    raise SystemExit(2)
except ImportError as _exc:                      # pragma: no cover - 只在文件缺失时发生
    school_auth = None                              # type: ignore[assignment]
    _AUTH_IMPORT_ERROR = _exc
else:
    _AUTH_IMPORT_ERROR = None

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
CFG = _CFG                                 # 学校相关的一切都在这里（见 school_config.py）

HOST = CFG.host
PORT = CFG.port                            # 测试时会改它（指向本地假学校）
BASE = CFG.base_url
APP = CFG.base_path

PATH_VOLUNTEER = CFG.path("volunteer")
PATH_CAPACITY = CFG.path("capacity")
PATH_RESULT = CFG.path("result")
PATH_STATUS = CFG.path("status")
PATH_SYSPARAM = CFG.path("sysparam")
PATH_PROGRAM = CFG.path("program")
PATH_STUDENT = CFG.path("student")
# 只用来读 HTTP Date 头做对时的轻量页面（默认取站点根路径）
TIME_PATH = str(CFG.school.get("time_path") or "/")

UA = str(CFG.http.get("user_agent"))

BEIJING = timezone(timedelta(hours=float(CFG.raw.get("timezone_offset_hours", 8))))

# 候选教学班来自配置（config.json 的 course.candidates），顺序 = 志愿优先级。
# 元组第三项是"冲突组"（星期-节次）：同一组互相冲突、最多只能中一个；
# 跨组若不冲突，交替提交会真的同时选上两门，所以脚本按组串行。
DEFAULT_CANDIDATES = CFG.candidates

# 提交报文里几个系统相关的字段（不同学校取值不同，写在 config.json 的 course 段里）
CLASS_TYPE = str(CFG.course.get("class_type") or "")
IS_MAJOR = str(CFG.course.get("is_major") or "1")
# 课程目录检索串模板：{keyword} 会被替换成课程名
QUERY_CONTENT = str(CFG.course.get("query_content") or "{keyword}")

# 服务端返回文案分类（中文教务系统常见措辞 + 实测响应；换学校可按需增删）
SUCCESS_WORDS = ("添加选课志愿成功", "添加选课成功", "选课成功")
ALREADY_WORDS = ("已经选过", "已选过", "已经选择", "重复选课", "已存在", "已选该课程", "已经选课")
FULL_WORDS = (
    "超过课容量", "该课程超过课容量", "课容量已满", "课程容量已满", "超过课程容量",
    "选课人数已满", "课程人数已满", "教学班容量已满", "该教学班已满", "容量已满", "人数已满",
)
CONFLICT_WORDS = ("时间冲突", "上课时间冲突", "与已选课程时间冲突", "课程冲突")
WINDOW_WORDS = (
    "当前时间不在选课开放时间范围内", "不在选课开放时间", "未在选课开放时间",
    "不在开放时间范围内", "不在选课时间", "不在补选时间", "非选课时间",
    "未到选课时间", "选课时间未到", "选课时间已过", "选课尚未开始", "未开放", "已结束", "已截止",
)
BUSY_WORDS = (
    "系统繁忙", "服务繁忙", "请稍后再试", "请求频繁", "操作频繁", "网络繁忙",
    # 实测：20:00 放课瞬间学校会对高频请求直接限流
    "请求过快", "请求太快", "请求过于频繁", "访问过快", "访问过于频繁",
    "提交过快", "提交过于频繁", "too many requests",
)
# 学校在放课前后会短暂重排数据，此时任何提交都会被拒，属于可重试
INIT_WORDS = (
    "系统正在初始化", "正在初始化", "系统初始化", "请稍候", "请稍后",
    "系统维护", "正在处理", "系统升级",
)
TERMINAL_WORDS = (
    "超过学分", "学分已满", "学分已达上限", "学分已达到上限",
    "选课门数已达上限", "选课门数已达到上限", "志愿数已达上限", "志愿数已达到上限",
    "不是选课对象", "无权限", "不能选课",
)
EXPIRED_WORDS = (
    "登录超时", "未登录", "请重新登录", "登录已过期", "未登录用户", "登录状态已过期",
    # 实测：volunteer.do 被限流后，学校会把会话作废，之后每一发都返回这句。
    # 它不含"未登录"字样，之前靠巧合才被归到过期，必须显式登记。
    "请求数据与登录者身份不一致", "身份不一致", "非法请求",
)

DEBUG_WRITES = os.environ.get("GRAB_DEBUG_WRITES") == "1"   # 逐发打印写请求（调限流用）

V_SUBMITTED = "SUBMITTED"      # code=1，学校已受理，进入异步处理
V_DUPLICATE = "DUPLICATE"      # 已选过 / 重复选课 -> 需复核
V_FULL = "FULL"                # 容量满 -> 换下一个候选
V_CONFLICT = "CONFLICT"        # 时间冲突 -> 该候选终止
V_TERMINAL = "TERMINAL"        # 学分/权限等终态失败 -> 该候选终止
V_WINDOW = "WINDOW_CLOSED"     # 未到放课时间 -> 继续等
V_BUSY = "BUSY"                # 系统繁忙 -> 退避重试
V_OVERLOAD = "OVERLOAD"        # 网关 5xx / 超时 / HTML 错误页 -> 服务器过载，保持节奏重试
V_EXPIRED = "EXPIRED"          # 会话过期 -> 停止，要求重新提供 cookie
V_UNKNOWN = "UNKNOWN"


def _sleep(seconds: float, state: "State | None" = None, step: float = 0.2) -> None:
    """可被打断的 sleep：手动停止后最多再等 `step` 秒就返回。"""
    end = time.time() + max(0.0, seconds)
    while True:
        if state is not None and state.stopped():
            return
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(step, left))


def install_stop_handler(state: "State") -> None:
    """Ctrl-C / kill 变成"优雅停止"：不发新请求、收尾、打印结果。

    第一次收到信号只是请求停止；如果卡住了，再按一次直接退出。
    """
    def handler(signum, _frame):
        if state.stopped():
            log("\n再次收到中止信号 —— 立即退出。")
            os._exit(130)
        state.stop()
        log(f"\n收到中止信号（{signum}）：不再发新的写请求，正在收尾并打印结果…"
            f"（再按一次 Ctrl-C 立即退出）")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            pass


def log(msg: str = "") -> None:
    print(msg, flush=True)


def ts() -> str:
    return datetime.now(BEIJING).strftime("%H:%M:%S.%f")[:-3]


def info(msg: str) -> None:
    log(f"[{ts()}] {msg}")


# ==========================================================================
# 一、Chrome Cookie（每次运行都重新读，不缓存）
# ==========================================================================
def _aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-CBC 解密；优先 cryptography，缺失时回落到 openssl 命令行。"""
    iv = b" " * 16
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        proc = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-nopad",
             "-K", key.hex(), "-iv", iv.hex()],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "需要 cryptography 或 openssl 才能解密 Chrome Cookie："
                + proc.stderr.decode("utf-8", "replace")[:200]
            )
        return proc.stdout
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    except TypeError:  # 老版本 cryptography 需要显式 backend
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    return dec.update(data) + dec.finalize()


def _unpad(blob: bytes) -> bytes:
    if blob:
        pad = blob[-1]
        if 1 <= pad <= 16:
            return blob[:-pad]
    return blob


def _derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1, 16)


def _keyring_password() -> bytes | None:
    """从 Secret Service 取 'Chrome Safe Storage'。失败返回 None。"""
    try:
        import dbus
    except ImportError:
        return None
    try:
        bus = dbus.SessionBus()
        svc = bus.get_object("org.freedesktop.secrets", "/org/freedesktop/secrets")
        iface = dbus.Interface(svc, "org.freedesktop.Secret.Service")
        _, session = iface.OpenSession("plain", dbus.String("", variant_level=1))
        props = dbus.Interface(svc, "org.freedesktop.DBus.Properties")
        for cpath in props.Get("org.freedesktop.Secret.Service", "Collections"):
            cobj = bus.get_object("org.freedesktop.secrets", cpath)
            cprops = dbus.Interface(cobj, "org.freedesktop.DBus.Properties")
            if cprops.Get("org.freedesktop.Secret.Collection", "Locked"):
                continue
            for ipath in cprops.Get("org.freedesktop.Secret.Collection", "Items"):
                iobj = bus.get_object("org.freedesktop.secrets", ipath)
                iprops = dbus.Interface(iobj, "org.freedesktop.DBus.Properties")
                if str(iprops.Get("org.freedesktop.Secret.Item", "Label")) == "Chrome Safe Storage":
                    item = dbus.Interface(iobj, "org.freedesktop.Secret.Item")
                    return bytes(item.GetSecret(session)[2])
    except Exception as exc:  # noqa: BLE001 - keyring 不可用是正常情况
        info(f"keyring 读取失败（将回落到 peanuts）: {exc}")
    return None


def _decrypt_value(enc: bytes, keys: list[tuple[str, bytes]], host: str) -> str | None:
    if not enc:
        return ""
    if enc[:3] not in (b"v10", b"v11"):
        try:
            return enc.decode("utf-8")
        except UnicodeDecodeError:
            return None
    host_hash = hashlib.sha256(host.encode()).digest()
    for _label, key in keys:
        try:
            blob = _unpad(_aes_cbc_decrypt(key, enc[3:]))
        except Exception:  # noqa: BLE001
            continue
        if blob.startswith(host_hash):        # 新版 Chrome 在明文前加 sha256(host_key)
            blob = blob[32:]
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def chrome_cookies(domain_sub: str | None = None,
                   profile: str | None = None) -> list[dict]:
    """从 Chrome 的 Cookies 库导出指定域名的 cookie（只读副本，不锁库）。"""
    domain_sub = domain_sub or CFG.cookie_domain
    profile = profile or os.path.expanduser("~/.config/google-chrome/Default")
    db = os.path.join(profile, "Cookies")
    if not os.path.exists(db):
        raise FileNotFoundError(f"找不到 Chrome Cookie 库: {db}")

    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(db, tmp)                     # Chrome 运行时库是锁的，复制出来读
    try:
        con = sqlite3.connect(tmp)
        rows = con.execute(
            "select host_key, name, encrypted_value, value, path, expires_utc, is_secure "
            "from cookies where host_key like ? order by host_key, name",
            (f"%{domain_sub}%",),
        ).fetchall()
        con.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    keys: list[tuple[str, bytes]] = []
    secret = _keyring_password()
    if secret:
        keys.append(("keyring", _derive_key(secret)))
    keys.append(("peanuts", _derive_key(b"peanuts")))

    # 用 host-hash 前缀判定哪把钥匙是对的
    probe = next(((e, h) for h, _n, e, _v, _p, _x, _s in rows
                  if e[:3] in (b"v10", b"v11") and len(e) > 40), None)
    if probe:
        want = hashlib.sha256(probe[1].encode()).digest()
        good = [(lbl, k) for lbl, k in keys
                if _unpad(_aes_cbc_decrypt(k, probe[0][3:])).startswith(want)]
        if good:
            keys = good

    now = time.time()
    out: list[dict] = []
    for host, name, enc, plain, path, expires, secure in rows:
        if expires:                            # Chrome 纪元: 1601-01-01 起的微秒
            if expires / 1_000_000 - 11644473600 < now:
                continue
        value = plain or _decrypt_value(enc, keys, host)
        if value:
            out.append({"host": host, "name": name, "value": value,
                        "path": path or "/", "secure": bool(secure)})
    return out


def cookie_header_from_chrome(profile: str | None = None) -> tuple[str, int]:
    cookies = chrome_cookies(None, profile)
    if not cookies:
        raise RuntimeError(f"Chrome 里没有 {CFG.cookie_domain} 的 Cookie —— 请先在 Chrome 登录选课系统")
    names = {c["name"] for c in cookies}
    for want in CFG.expect_cookies:
        if want not in names:
            info(f"警告: 未见到 {want} Cookie，当前有 {sorted(names)}")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies), len(cookies)


def detect_student_code(profile: str | None = None) -> str | None:
    """从 Chrome 的 Session Storage 里捞 studentInfo.code（纯本地读取）。"""
    profile = profile or os.path.expanduser("~/.config/google-chrome/Default")
    root = os.path.join(profile, "Session Storage")
    if not os.path.isdir(root):
        return None
    pat = re.compile(r'"code"\s*:\s*"(\d{8,12})"')
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        try:
            blob = open(path, "rb").read()
        except OSError:
            continue
        for enc in ("utf-16-le", "utf-8"):
            text = blob.decode(enc, "ignore")
            idx = text.find("studentInfo")
            if idx < 0:
                continue
            hit = pat.search(text[idx:idx + 8000])
            if hit:
                return hit.group(1)
    return None


# ==========================================================================
# 二、学校会话（只读预检 + 提交）
# ==========================================================================
class School:
    """极简学校客户端。提交路径与只读路径分开，避免误用。

    会话恢复（`auth` 不为 None 时）：只读请求一旦被学校判为"会话过期"，
    就自动重新登录一次并重放该请求；提交请求**不**自动重放（见 `json_post`）。
    """

    def __init__(self, token: str, cookie: str, referer: str):
        self.token = token
        self.cookie = cookie
        self.referer = referer
        self.code = ""          # 学号，预检阶段填入
        self.auth = None        # school_auth.ReloginManager | None
        self.pacer: WritePacer | None = None   # 写请求节拍器（首发与重试共用）
        self.write_timeout = 4.0               # 单发写请求的最长占用（秒），见 post()
        self.read_timeout = 15.0               # 只读请求的超时（复核/查空位可以耐心点）
        self.relogins = 0       # 本次运行成功自动重登录的次数
        self._conn: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()
        self._recover_lock = threading.Lock()
        self._recovering = False

    # ---- 会话 ----
    def use_cookie(self, cookie: str) -> None:
        """直接换一套 Cookie（读 Chrome / --cookie 用）。"""
        with self._lock:
            self.cookie = cookie

    def adopt(self, session) -> None:
        """换用一次自动登录拿到的新会话：token、Cookie、Referer 全部跟着换。"""
        with self._lock:
            self.token = session.token
            self.cookie = session.cookie
            self.referer = session.referer()

    def probe(self) -> tuple[bool, dict]:
        """只读接口确认会话是否有效。返回 (是否有效, 原始返回)。"""
        path = PATH_STUDENT.format(code=self.code) + f"?timestamp={int(time.time() * 1000)}"
        try:
            payload, status, text = self._post_json(path)
        except Exception as exc:  # noqa: BLE001 - 网络问题不代表会话死了
            return False, {"msg": f"网络错误: {exc}"}
        ok = not _is_expired(payload, status, text) and str(payload.get("code")) == "1"
        return ok, payload

    def recover(self, reason: str = "") -> bool:
        """会话死了就自动登回来。返回 True = 现在这套会话是好的。

        进入时先自己复核一遍：并发路径（复核线程 + 重试线程）可能同时发现会话过期，
        第二个进来的会发现"已经好了"，就不会再打一次登录接口。
        """
        if self.auth is None or not self.code:
            return False
        with self._recover_lock:
            ok, _payload = self.probe()
            if ok:
                return True
            self._recovering = True
            try:
                session = self.auth.relogin(reason)
                if session is None:
                    return False
                self.adopt(session)
                ok, payload = self.probe()
                if not ok:
                    info(f"[auth] 新会话复验未通过: {payload.get('msg')}")
                    return False
            finally:
                self._recovering = False
            self.relogins += 1
            info(f"[auth] ✓ 会话就绪（本次运行第 {self.relogins} 次自动登录），"
                 f"新 token {self.token[:8]}…{self.token[-4:]}")
            return True

    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Cookie": self.cookie,
            "Origin": BASE,
            "Referer": self.referer,
            "User-Agent": UA,
            "X-Requested-With": "XMLHttpRequest",
            "token": self.token,
        }

    def _new_conn(self) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=15)
        conn.connect()
        try:
            conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        return conn

    def date_sample(self) -> tuple[int, float, float] | None:
        """GET 一个轻量页面，返回 (服务器整秒, 本机中点, RTT)。只读。

        只用来读 HTTP Date 头做时钟对齐。
        """
        with self._lock:
            for _ in (1, 2):
                try:
                    if self._conn is None:
                        self._conn = self._new_conn()
                    t0 = time.time()
                    self._conn.request("GET", f"{TIME_PATH}?_={int(t0 * 1000)}", headers={
                        "Host": HOST, "User-Agent": UA, "Accept": "*/*",
                        "Cache-Control": "no-cache", "Pragma": "no-cache",
                    })
                    resp = self._conn.getresponse()
                    resp.read()
                    t1 = time.time()
                    raw = resp.getheader("Date")
                    if not raw:
                        return None
                    stamp = _parse_http_date(raw)
                    if stamp is None:
                        return None
                    return stamp, (t0 + t1) / 2.0, t1 - t0
                except Exception:  # noqa: BLE001 - 连接坏了就重连一次
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
        return None

    def post(self, path: str, data: dict | None = None, attempts: int = 2,
             timeout: float | None = None) -> tuple[int, str]:
        """带 keep-alive 的 POST。

        `attempts=1` 用于写请求：放课瞬间服务器过载时，一次写请求最多只能占用
        `timeout` 秒，绝不允许在这里内部重试两次把窗口吃光（实测 15s 超时下
        一轮三发要 45 秒，整个放课窗口就没了）。重试交给外层的轮次节奏。
        """
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        last: Exception | None = None
        for attempt in range(max(1, attempts)):
            with self._lock:
                try:
                    if self._conn is None:
                        self._conn = self._new_conn()
                    conn = self._conn
                    # 每条请求都显式设自己的超时：写请求用过 2s 之后，
                    # 同一条 keep-alive 连接上的只读请求不能被残留的短超时误伤。
                    conn.sock.settimeout(timeout or self.read_timeout)
                    conn.request("POST", path, body=body, headers=self.headers())
                    resp = conn.getresponse()
                    text = resp.read().decode("utf-8", "replace")
                    return resp.status, text
                except Exception as exc:  # noqa: BLE001 - 连接坏了就重连
                    last = exc
                    try:
                        if self._conn:
                            self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._conn = None
        raise RuntimeError(f"请求 {path} 失败: {last}")

    def _post_json(self, path: str, data: dict | None = None, attempts: int = 2,
                   timeout: float | None = None) -> tuple[dict, int, str]:
        status, text = self.post(path, data, attempts=attempts, timeout=timeout)
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {}
        return payload, status, text

    def json_post(self, path: str, data: dict | None = None, *,
                  recover: bool = True, attempts: int = 2,
                  timeout: float | None = None) -> tuple[dict, int, str]:
        """POST + 解析 JSON。

        `recover=True`（只读路径）：被学校判"会话过期"时自动重登录一次并重放请求。
        `recover=False`（提交路径）：**绝不自动重放** —— 一发选课请求要么发一次，
        要么不发；"过期"的判定与后续动作留给重试循环显式处理，避免重复提交。
        """
        payload, status, text = self._post_json(path, data, attempts=attempts, timeout=timeout)
        if (recover and not self._recovering and self.auth is not None
                and _is_expired(payload, status, text)):
            name = path.split("/")[-1].split("?")[0]
            info(f"[auth] 只读接口 {name} 报会话过期，尝试自动重登录…")
            if self.recover(f"{name} 报会话过期"):
                payload, status, text = self._post_json(path, data, attempts=attempts,
                                                        timeout=timeout)
        return payload, status, text

    # ---- 只读 ----
    def student(self, code: str, *, recover: bool = True) -> dict:
        payload, _s, _t = self.json_post(
            PATH_STUDENT.format(code=code) + f"?timestamp={int(time.time() * 1000)}",
            recover=recover)
        return payload

    def enrolled_ids(self) -> set[str]:
        payload, _s, _t = self.json_post(
            f"{PATH_RESULT}?timestamp={int(time.time() * 1000)}&studentCode={self.code}")
        if _is_expired(payload, _s, _t):
            raise SessionExpired("courseResult 返回登录过期")
        if str(payload.get("code")) != "1":
            raise RuntimeError(f"已选课程查询失败: {payload.get('msg')}")
        rows = payload.get("dataList") or []
        return {str(r.get("teachingClassID") or "").strip() for r in rows if r}

    def capacity(self, tc_id: str, batch: str) -> dict:
        payload, _s, _t = self.json_post(
            PATH_CAPACITY, {"teachingClassId": tc_id, "batchCode": batch})
        if _is_expired(payload, _s, _t):
            raise SessionExpired("capacity 返回登录过期")
        return payload.get("data") or {}

    def catalog_candidates(self, code: str, batch: str, campus: str,
                               keyword: str = "") -> list[dict]:
        """从学校课程目录解析候选教学班（只读，用于建白名单）。"""
        setting = {
            "data": {
                "studentCode": code, "campus": campus, "electiveBatchCode": batch,
                "isMajor": IS_MAJOR, "teachingClassType": CLASS_TYPE,
                "checkConflict": "2", "checkCapacity": "2",
                "queryContent": QUERY_CONTENT.replace("{keyword}", keyword),
            },
            "pageSize": "50", "pageNumber": "0", "order": "", "orderBy": "courseNumber",
        }
        payload, _s, _t = self.json_post(
            PATH_PROGRAM, {"querySetting": json.dumps(setting, ensure_ascii=False,
                                                      separators=(",", ":"))})
        out: list[dict] = []
        for course in payload.get("dataList") or []:
            if keyword not in str(course.get("courseName") or ""):
                continue
            for tc in course.get("tcList") or []:
                out.append({
                    "tc_id": str(tc.get("teachingClassID") or ""),
                    "index": str(tc.get("courseIndex") or ""),
                    "teacher": str(tc.get("teacherName") or ""),
                    "place": str(tc.get("teachingPlace") or ""),
                    "course_number": str(course.get("courseNumber") or ""),
                    "credit": str(course.get("credit") or "0"),
                    "is_full": str(tc.get("isFull") or ""),
                    "is_conflict": str(tc.get("isConflict") or ""),
                    "is_main": str(tc.get("isMainSelectObject") or "0"),
                })
        return out

    def submit(self, code: str, batch: str, tc_id: str, campus: str,
               tc_type: str | None = None, timeout: float | None = None
               ) -> tuple[dict, int, str, float]:
        """唯一会改变学校状态的调用。字段与学校 grablessons.js 完全一致。

        发出去之前先过 WritePacer：超过"每秒 3 发"的硬上限时在这里等，
        而不是让学校回一句"请求过快"把整个会话打死。
        """
        if self.pacer is not None:
            self.pacer.acquire()
        add = {"data": {
            "operationType": "1",
            "studentCode": code,
            "electiveBatchCode": batch,
            "teachingClassId": tc_id,
            "isMajor": IS_MAJOR,
            "campus": campus,
            "teachingClassType": tc_type,
        }}
        body = {"addParam": json.dumps(add, ensure_ascii=False, separators=(",", ":"))}
        t0 = time.time()
        payload, status, text = self.json_post(PATH_VOLUNTEER, body, recover=False,
                                               attempts=1,
                                               timeout=timeout or self.write_timeout)
        dt = (time.time() - t0) * 1000
        self._debug_write(tc_id, payload, status, text, dt)
        return payload, status, text, dt

    def submit_fresh(self, code: str, batch: str, tc_id: str, campus: str,
                     tc_type: str | None = None, timeout: float | None = None
                     ) -> tuple[dict, int, str, float]:
        """一发写请求独占一条**全新短连接**。

        给"多班错开同时发"用：过载时一条挂住的连接只会拖死它自己，
        不会让同轮其它候选跟着一起等（实测这是 20:00 窗口最大的时间黑洞）。
        连接用完就关 —— 放课窗口里省下的重连时间远不如"不被拖住"值钱。
        """
        if self.pacer is not None:
            self.pacer.acquire()
        body = _submit_body(tc_id, code, batch, campus, tc_type)
        t0 = time.time()
        conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout or self.write_timeout)
        try:
            conn.connect()
            try:
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass
            conn.request("POST", PATH_VOLUNTEER, body=body, headers=self.headers())
            resp = conn.getresponse()
            status = resp.status
            text = resp.read().decode("utf-8", "replace")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        dt = (time.time() - t0) * 1000
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {}
        if DEBUG_WRITES:
            try:
                verdict, _msg = classify(payload, status, text)
                info(f"[dbg] 写(短连) {tc_id[-3:]} 于 "
                     f"{datetime.now(BEIJING).strftime('%H:%M:%S.%f')[:-3]}"
                     f" → {verdict} {dt:.0f}ms  {text[:60]}")
            except Exception:  # noqa: BLE001
                pass
        return payload, status, text, dt

    def _debug_write(self, tc_id: str, payload: dict, status: int, text: str,
                     dt: float) -> None:
        if DEBUG_WRITES:
            # 调试输出绝不能影响提交路径本身（这里踩过一次：snapshot 缺失把整轮炸掉，
            # 重试循环把每一发都当成"提交异常"，白扔了 12 发额度）。
            try:
                verdict, _msg = classify(payload, status, text)
                info(f"[dbg] 写 {tc_id[-3:]} 于 "
                     f"{datetime.now(BEIJING).strftime('%H:%M:%S.%f')[:-3]}"
                     f" → {verdict} {dt:.0f}ms  窗口内 "
                     f"{self.pacer.snapshot() if self.pacer else '?'} 发  {text[:70]}")
            except Exception as exc:  # noqa: BLE001
                info(f"[dbg] 调试输出失败（已忽略）: {exc}")


class SessionExpired(RuntimeError):
    pass


def _submit_body(tc_id: str, code: str, batch: str, campus: str,
                 tc_type: str | None = None) -> bytes:
    """写请求的报文体（首发、重试、错开发都共用这一份，字段与前端提交的一致）。"""
    add = {"data": {
        "operationType": "1",
        "studentCode": code,
        "electiveBatchCode": batch,
        "teachingClassId": tc_id,
        "isMajor": IS_MAJOR,
        "campus": campus,
        "teachingClassType": tc_type or CLASS_TYPE,
    }}
    return urllib.parse.urlencode(
        {"addParam": json.dumps(add, ensure_ascii=False, separators=(",", ":"))}
    ).encode("utf-8")


class WritePacer:
    """全局写请求节拍器：**所有**写请求（首发那几发、重试循环的每一发）都要先过它。

    实测模型（2026-09-25 用真实满员教学班复测，2/2 复现）：
        滚动 1 秒内最多 3 发写请求，第 4 发必定返回
        「请求过快，请登录后再试」，**并且当场作废整个会话**（之后每发都是
        「请求数据与登录者身份不一致」）。
    这次能用真实 ID 测出结论，是因为之前的探针一律用**假教学班 ID**：假 ID 在写库前
    就被拒，走不到真实的写路径，于是"3 发/秒"这个数字一直没被证伪。真实死因是：

        首发 1 发（T+0.29s） + 重试循环第一轮 3 发（T+1.0s）
        = 1.05 秒内 4 发 → 第 4 发被限流 → 会话死 → 任务在放课瞬间自杀。

    节拍器把"一秒钟最多 3 发"变成代码里的硬约束，任何调用路径都绕不过去：
    首发 3 发同时出手没问题（第一秒的额度正好是 3），但重试循环必须等满一个窗口
    才轮到它 —— 而不是像以前那样按 `fire_at + interval` 盲目开打。
    """

    def __init__(self, per_window: int = 3, window: float = 1.0, margin: float = 0.15,
                 min_gap: float = 0.0) -> None:
        self.per_window = per_window
        self.window = window
        self.margin = margin            # 覆盖 RTT 与两端时钟误差，宁可慢一点
        self.min_gap = min_gap          # 相邻两发之间的最小间隔（错开发，见 concurrent_probe）
        # 保守窗口 = 服务端窗口 + 安全边界。踩过一次边界：第 4 发正好在第 1 发之后
        # 1.000s 发出，我们这边已经"过期"了，学校那边还算在窗口内 → 4 发 → 当场被踢。
        # 所以"遗忘"一条记录也必须等到 window+margin 之后。
        self.span = window + margin
        self._times: collections.deque = collections.deque()
        self._last = 0.0
        self._lock = threading.Lock()
        self.waits = 0
        self.total = 0

    def snapshot(self) -> int:
        """当前滚动窗口内已经用掉几个名额（调试用）。"""
        with self._lock:
            now = time.time()
            return len([t for t in self._times if now - t < self.span])

    def acquire(self) -> float:
        """拿到一个写请求名额；拿不到就在这里等到能拿。返回等待秒数。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                while self._times and now - self._times[0] >= self.span:
                    self._times.popleft()
                # 两个约束同时成立才放行：① 滚动窗口内不超过 per_window 发；
                # ② 与上一发至少隔开 min_gap —— 实测 0.03s 内连发两发，学校会回
                #    code=2 且 msg 为空的并发拒绝（等于白打一发），0.06s 以上才稳。
                gap_left = self.min_gap - (now - self._last) if self._last else 0.0
                if len(self._times) < self.per_window and gap_left <= 0:
                    self._times.append(now)
                    self._last = now
                    self.total += 1
                    return waited
                sleep_for = max(gap_left,
                                self.span - (now - self._times[0])
                                if len(self._times) >= self.per_window else 0.0)
            chunk = min(max(sleep_for, 0.005), 0.25)
            self.waits += 1
            time.sleep(chunk)
            waited += chunk


def _is_expired(payload: dict, status: int, text: str) -> bool:
    if status in (301, 302, 401, 403):
        return True
    if str(payload.get("code")) in ("302", "401", "403"):
        return True
    low = (text or "").lower()
    if "student/check/login" in low:
        return True
    return "vtoken" in low and "loginpwd" in low


def classify(payload: dict, status: int, text: str) -> tuple[str, str]:
    """把学校返回归类成一个动作。"""
    msg = str(payload.get("msg") or "").strip()
    code = str(payload.get("code") or "")
    hay = f"{msg}\n{text}"

    # 限流 / 初始化必须最先判：实测放课瞬间学校会返回
    # 「请求过快，请登录后再试」和「选课系统正在初始化,请稍候...」，
    # 前者带「登录」字样、且常常伴随 code=302。若按会话过期处理会直接放弃整个任务，
    # 而它们其实都是"退避后重试"就能过的。
    if any(w in hay for w in BUSY_WORDS):
        return V_BUSY, msg
    if any(w in hay for w in INIT_WORDS):
        return V_BUSY, msg

    # 放课瞬间服务器被打爆时，写请求根本回不来或者被网关拦掉。这既不是业务失败，
    # 也不是会话过期 —— 必须单独归类，否则会被当成 UNKNOWN 而丢掉信息。
    if status >= 500:
        return V_OVERLOAD, msg or f"HTTP {status}（网关错误）"
    if status == 0:
        return V_OVERLOAD, msg or "没有拿到响应（超时/连接被断）"
    low = (text or "").lower().lstrip()
    if not payload and (low.startswith("<") or low.startswith("<!doctype")):
        return V_OVERLOAD, "网关返回了 HTML 错误页"

    if _is_expired(payload, status, text):
        return V_EXPIRED, msg or "登录已过期"
    if code == "1" or any(w in hay for w in SUCCESS_WORDS):
        return V_SUBMITTED, msg
    if any(w in hay for w in ALREADY_WORDS):
        return V_DUPLICATE, msg
    if any(w in hay for w in CONFLICT_WORDS):
        return V_CONFLICT, msg
    if any(w in hay for w in FULL_WORDS):
        return V_FULL, msg
    if any(w in hay for w in WINDOW_WORDS):
        return V_WINDOW, msg
    if any(w in hay for w in TERMINAL_WORDS):
        return V_TERMINAL, msg
    if any(w in hay for w in EXPIRED_WORDS):
        return V_EXPIRED, msg
    return V_UNKNOWN, msg or text[:80]


# ==========================================================================
# 三、服务器时钟
# ==========================================================================
def _parse_http_date(raw: str) -> int | None:
    """把 HTTP Date 头解析成整秒 epoch；失败返回 None。"""
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def server_offset(school: School, samples: int = 25) -> tuple[float, float]:
    """用 HTTP Date 头做严格区间估计。

    返回 (offset, half_width_ms)，offset = 服务器时间 - 本机时间（秒）。

    **不用 JSON 里的 timestamp 字段。** 实测该字段在 RTT 仅 28ms 的情况下
    摆动超过 1100ms，甚至出现时间倒流（后端多台机器时钟不同步 / 缓存），
    取它做对时会把偏移算反、算错一个数量级。

    Date 头只有 1 秒分辨率，但「S <= 服务器时间 < S+1」这个约束对多个样本
    求交集后，可以把偏移夹到 RTT 量级。Date 是在 [t0,t1] 之间某一刻生成的，
    所以每条约束要放宽 ±RTT/2，否则交集会被压成空集。
    """
    got_list: list[tuple[int, float, float]] = []
    for _ in range(samples):
        got = school.date_sample()
        if got is not None:
            got_list.append(got)
        time.sleep(0.02)
    if not got_list:
        return 0.0, -1.0

    # 网格投票：每个样本给出「offset 必须落在 [sec-mid-rtt/2, sec+1-mid+rtt/2)」这条约束，
    # 取被最多样本同时覆盖的偏移。
    # 不用增量求交 —— 那是贪心且依赖顺序的：某一步走偏就会把正确样本当离群点丢掉，
    # 然后锁死在一个错误区间上（实测会给出 ±6ms 这种假精确值）。
    step = 0.005
    grid_lo, grid_hi = -1.5, 1.5
    n_bins = int(round((grid_hi - grid_lo) / step)) + 1
    votes = [0] * n_bins
    for sec, mid, rtt in got_list:
        a = sec - mid - rtt / 2.0
        b = (sec + 1) - mid + rtt / 2.0
        for i in range(n_bins):
            if a <= grid_lo + i * step < b:
                votes[i] += 1

    best = max(votes)
    agree = best / len(got_list)
    if agree < 0.6:                   # 样本之间对不上，不猜
        return 0.0, -2.0
    idx = [i for i, v in enumerate(votes) if v == best]
    est = grid_lo + (idx[0] + idx[-1]) / 2.0 * step
    half = (idx[-1] - idx[0]) * step * 500.0
    if abs(est) > 0.5:                # 超出合理范围，视为不可信
        return 0.0, -2.0
    return est, max(half, step * 500.0)


def resolve_fire_time(at: str, offset: float, early: float, *,
                      now: bool = False, tomorrow: bool = False) -> tuple[float, str]:
    """决定什么时候出手，并返回一句人话说明。

    这里刻意**不**沿用「已过就取明天」的老逻辑：放课之后才是捡漏的时段
    （有人退课就漏位子），20:00 后重启脚本必须立刻开打，而不是干等 24 小时。
    要等到明天请显式加 --tomorrow。
    """
    if now:
        return time.time(), "立即（--now）"
    try:
        hh, mm, ss = (int(x) for x in at.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
            raise ValueError
    except ValueError:
        raise SystemExit(f"✗ --at 格式应为 HH:MM:SS，收到 {at!r}") from None

    cur = datetime.now(BEIJING)
    when = cur.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if when > cur:
        return when.timestamp() - offset - early, f"今天 {at}"
    if tomorrow:
        return (when + timedelta(days=1)).timestamp() - offset - early, f"明天 {at}"
    return time.time(), f"今天 {at} 已过 → 立即开始（想等明天请加 --tomorrow）"


def time_group(place: str) -> str:
    """从课表文本（如 '5-18周 星期三 3-5节 某楼101'）里提取冲突组 '星期三-3-5'。

    同一冲突组的教学班互相撞时间，学校最多只让中一个，所以并发发是安全的；
    不同组（周三 vs 周五）并发则可能真的同时选上两门。
    """
    text = str(place or "").strip()
    m = re.search(
        r"(星期[一二三四五六日天]|周[一二三四五六日天]).{0,12}?([0-9]+\s*[-至]\s*[0-9]+)", text
    )
    if not m:
        return ""
    day = m.group(1).replace("周", "星期")
    periods = re.sub(r"\s+", "", m.group(2)).replace("至", "-")
    return f"{day}-{periods}"


# ==========================================================================
# 四、首发：预热 TCP + 自旋到点 + 一次性写出
# ==========================================================================
def build_wire(school: School, code: str, batch: str, tc_id: str,
               campus: str, tc_type: str | None = None) -> bytes:
    """把整个 HTTP 请求预序列化成字节，放课瞬间直接 write。"""
    add = {"data": {
        "operationType": "1", "studentCode": code, "electiveBatchCode": batch,
        "teachingClassId": tc_id, "isMajor": IS_MAJOR, "campus": campus,
        "teachingClassType": tc_type or CLASS_TYPE,
    }}
    body = urllib.parse.urlencode(
        {"addParam": json.dumps(add, ensure_ascii=False, separators=(",", ":"))}
    ).encode("utf-8")
    head = "\r\n".join([
        f"POST {PATH_VOLUNTEER} HTTP/1.1",
        f"Host: {HOST}",
        f"User-Agent: {UA}",
        "Accept: application/json, text/javascript, */*; q=0.01",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        f"Content-Length: {len(body)}",
        f"Origin: {BASE}",
        f"Referer: {school.referer}",
        f"Cookie: {school.cookie}",
        f"token: {school.token}",
        "X-Requested-With: XMLHttpRequest",
        "Connection: keep-alive",
    ]) + "\r\n\r\n"
    return head.encode("utf-8") + body


def _dechunk(blob: bytes) -> bytes:
    out, rest = b"", blob
    while True:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            return out + line
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return out + line + rest
        if size == 0:
            return out
        out += rest[:size]
        rest = rest[size + 2:]


def read_http(sock: socket.socket, timeout: float = 10.0) -> tuple[int, bytes]:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, sep, body = buf.partition(b"\r\n\r\n")
    if not sep:
        return 0, buf
    status = 0
    first = head.split(b"\r\n", 1)[0].split(b" ")
    if len(first) >= 2 and first[1].isdigit():
        status = int(first[1])
    hdrs: dict[bytes, bytes] = {}
    for line in head.split(b"\r\n")[1:]:
        k, s, v = line.partition(b":")
        if s:
            hdrs[k.strip().lower()] = v.strip()
    if hdrs.get(b"transfer-encoding", b"").lower() == b"chunked":
        while b"0\r\n\r\n" not in body:
            try:
                more = sock.recv(65536)
            except socket.timeout:
                break
            if not more:
                break
            body += more
        body = _dechunk(body)
    else:
        need = int(hdrs.get(b"content-length", b"0") or 0)
        while len(body) < need:
            try:
                more = sock.recv(65536)
            except socket.timeout:
                break
            if not more:
                break
            body += more
    return status, body


class State:
    """线程间共享状态。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.confirmed: str | None = None
        self.multi: list[str] = []          # 并行首发同时选上了多个（异常，需人工退课）
        self.blocked: set[str] = set()      # 该候选已明确失败，别再打
        self.stop_requested = False         # 手动停止（Ctrl-C / SIGTERM）
        self.hits: list[tuple[float, str, str, str]] = []   # (t, tc, verdict, msg)

    def record(self, tc: str, verdict: str, msg: str) -> None:
        with self.lock:
            self.hits.append((time.time(), tc, verdict, msg))

    def block(self, tc: str) -> None:
        with self.lock:
            self.blocked.add(tc)

    def finish(self, tc: str) -> None:
        with self.lock:
            self.done = True
            self.confirmed = tc

    def is_done(self) -> bool:
        with self.lock:
            return self.done

    def stop(self) -> None:
        """请求停止：不再发新的写请求，当前这一轮收尾后正常打印结果退出。"""
        with self.lock:
            self.stop_requested = True

    def stopped(self) -> bool:
        with self.lock:
            return self.stop_requested

    def blocked_ids(self) -> set[str]:
        with self.lock:
            return set(self.blocked)


def sniper(wire: bytes, tc_id: str, fire_at: float, state: State,
           out: list, idx: int, pacer: "WritePacer | None" = None,
           write_timeout: float = 4.0, gate: "threading.Event | None" = None,
           done: "threading.Event | None" = None, gate_timeout: float = 0.0) -> None:
    """预热 TCP，自旋到点，把整包写出去。

    建连刻意推迟到 T-3s：空转几十秒的 TCP 可能已被服务端或中间设备回收，
    那样首发会打在一条死连接上。3 秒既在 keep-alive 超时之内，又足够完成握手。

    `gate`/`done` 用来把多发串成"上一发响应回来再发下一发"：学校对同一个学生的写请求
    有并发互斥，上一发还在处理时打进去的请求只会拿到一个空 msg 的拒绝（实测，
    服务端处理 239ms 时固定 100ms 间隔的三发里有两发就是这种空回复）。
    `gate_timeout` 是等待上限 —— 服务器挂住时，最多多等这么久就照发下一发。
    """
    sock = None
    try:
        if gate is not None:
            # 等到上一发回来（或超出上限）。等待上限 = 距离自己该出手还剩的时间 + 宽限。
            gate.wait(timeout=max(0.0, fire_at - time.time()) + gate_timeout)
        while True:                          # 先睡到 T-3s
            left = (fire_at - 3.0) - time.time()
            if left <= 0:
                break
            time.sleep(min(0.25, left))

        # 建连：放课瞬间服务器可能因为过载丢 SYN / backlog 打满，建连失败不能让这一发
        # 直接没了 —— 重试到点为止（每次 1.5s 超时）。这是首发最容易被忽略的失效点。
        last_exc: Exception | None = None
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(1.5)
                sock.connect((HOST, PORT))    # 握手提前做完，不占用 T0 的 RTT
                break
            except OSError as exc:
                last_exc = exc
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
                if time.time() >= fire_at:
                    raise
                info(f"  首发#{idx} 建连失败（{exc}），重试到点…")
                time.sleep(0.05)
        if sock is None:
            raise last_exc or OSError("建连失败")

        t_conn = time.time()
        while True:                          # 自旋等待，避免 sleep 的调度抖动
            left = fire_at - time.time()
            if left <= 0:
                break
            if left > 0.002:
                time.sleep(left - 0.001)
        # 到点后再取写名额：首发这几发是第一个窗口的额度，正常情况下是零等待。
        # 这一步只是保证"任何路径都不会凑出第 4 发"，不会拖慢首发。
        if state.stopped():                  # 手动停了就不再出手
            out.append((idx, tc_id, 0, json.dumps({"error": "已手动停止"}).encode(), 0.0))
            info(f"  首发#{idx} 已取消（手动停止）")
            return
        if pacer is not None:
            pacer.acquire()
        t0 = time.time()
        sock.sendall(wire)
        status, body = read_http(sock, timeout=write_timeout)
        dt = (time.time() - t0) * 1000
        out.append((idx, tc_id, status, body, dt))
        info(f"  首发#{idx} 建连于 T{(t_conn - fire_at):+.2f}s，写出于 T{(t0 - fire_at) * 1000:+.1f}ms")
    except Exception as exc:  # noqa: BLE001 - 首发失败不影响主循环
        out.append((idx, tc_id, 0, json.dumps({"error": str(exc)}).encode(), 0.0))
        info(f"  首发#{idx} 失败（重试循环会兜底）: {exc}")
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if done is not None:
            done.set()


# ==========================================================================
# 五、复核与重试
# ==========================================================================
def session_dead(school: School, code: str) -> bool:
    """确认会话是不是真的死了。

    学校限流会返回「请求过快，请登录后再试」并把会话作废，这种"过期"有时是临时的。
    实测教训：19:59:59.22 一发被判过期就终止了整个任务，比放课早 780ms 就自杀了。
    所以过期必须用只读接口隔几秒复核，真死了才放弃。

    这里刻意用 recover=False 关掉自动重登录：本函数只负责回答"现在到底死没死"，
    救不救、怎么救由调用方决定（重试循环里那段有完整日志）。

    复核节奏看有没有自动登录兜底：有的话压到 0.4/0.8 秒 —— 放课窗口只有几十秒，
    而限流造成的"假过期"实测是**永久**的（之后每一发都是"身份不一致"），
    所以不需要在这里反复等；即使误判"死"，`recover()` 进去还会再复核一次，
    真活着就什么都不会发生，代价只是一次只读请求。
    """
    delays = (0.4, 0.8) if school.auth is not None else (1.0, 2.0, 4.0)
    for delay in delays:
        time.sleep(delay)
        try:
            payload = school.student(code, recover=False)
        except Exception:  # noqa: BLE001 - 网络问题不代表会话死了
            continue
        if str(payload.get("code")) == "1":
            return False
    return True


def has_slot(school: School, tc_id: str, batch: str) -> bool | None:
    """只读检查某个教学班是否还有非主选空位。

    返回 True=有空位、False=已满、None=查不到（查不到就照打，不要因为读失败而漏掉机会）。
    你在这门课属于非主选对象（isMainSelectObject=0），所以只看 nonMain 那一栏。

    **0/0 一律当成"查不到"。** 2026-09-25 20:00 的教训：放课瞬间系统会进入
    「正在初始化」，这段时间容量接口返回 total=0/used=0 —— 那是"没有数据"，
    不是"没有空位"。旧版把它算成 0-0>0=False（已满），于是脚本在整个放课窗口里
    安静地轮询了 56 秒，一发写请求都没发出去。
    """
    try:
        cap = school.capacity(tc_id, batch)
    except Exception:  # noqa: BLE001 - 读失败不该影响抢课
        return None
    total = cap.get("nonMainClassCapacity")
    used = cap.get("nonMainElectiveNumber")
    if total is None or used is None:
        return None
    try:
        total_i, used_i = int(total), int(used)
    except (TypeError, ValueError):
        return None
    if total_i <= 0:                 # 0=接口还没数据（初始化中），不是"满"
        return None
    return total_i - used_i > 0


def confirm(school: School, tc_id: str, attempts: int = 6,
            gap: float = 0.4) -> bool:
    """唯一真值：已选课程列表里出现完整 teachingClassID。"""
    for i in range(1, attempts + 1):
        try:
            if tc_id in school.enrolled_ids():
                return True
        except SessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            info(f"复核第 {i}/{attempts} 次异常: {exc}")
        if i < attempts:
            time.sleep(gap)
    return False


def process_ok(school: School, code: str, tries: int = 6) -> str:
    """按学校前端逻辑轮询 studentstatus.do：code 1=成功, -1=失败, 其他=处理中。"""
    for _ in range(tries):
        try:
            payload, _s, _t = school.json_post(PATH_STATUS, {"studentCode": code})
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
            continue
        c = str(payload.get("code") or "")
        if c == "1":
            return "ok"
        if c == "-1":
            return f"fail:{payload.get('msg')}"
        time.sleep(0.4)
    return "pending"


# ==========================================================================
# 六、主流程
# ==========================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="教务系统抢课 · 放课窗口精准首发（默认只预检，加 --live 才提交）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--url", default=None,
                   help="浏览器地址栏里带 token 的完整业务页 URL。"
                        "有凭据文件（自动登录）时可以不给 —— token 登录后会自己拿到")
    p.add_argument("--live", action="store_true",
                   help="真正提交选课请求。不加此参数只做只读预检与演练。")
    p.add_argument("--at", default="20:00:00",
                   help="首发时刻（北京时间 HH:MM:SS），默认 20:00:00")
    p.add_argument("--now", action="store_true", help="跳过等待，立刻开始打")
    p.add_argument("--tomorrow", action="store_true",
                   help="--at 已过时等到明天；默认是「已过就立刻开打」（放课后才有漏可捡）")
    p.add_argument("--early-ms", type=float, default=0.0,
                   help="正数=提前、负数=推后多少毫秒出手。默认 0：瞄准 20:00:00.000 正点，"
                        "因为放课是一个瞬间，早打等于白扔一发")
    p.add_argument("--student", default=None,
                   help="学号（默认从凭据文件 / Chrome 自动识别）")
    p.add_argument("--cookie", default=None, help="直接给 Cookie 字符串（默认读 Chrome）")
    p.add_argument("--profile", default=None, help="Chrome profile 目录")

    # ---- 自动登录 / 被踢重登录 ----
    g = p.add_argument_group("自动登录（学号+密码 → 会话，验证码用 captcha-model）")
    g.add_argument("--credentials", default=None,
                   help=f"凭据文件路径，默认 {school_auth.DEFAULT_CREDENTIALS if school_auth else '~/.config/course-grabber/credentials.json'}")
    g.add_argument("--password", default=None,
                   help="临时给一次密码（会出现在 ps / shell 历史里，日常请用凭据文件）")
    g.add_argument("--no-relogin", action="store_true",
                   help="关掉整条自动登录链路，回到「只读 Chrome Cookie」的老行为")
    g.add_argument("--no-chrome", action="store_true",
                   help="完全不读 Chrome Cookie；没凭据就直接失败（想验证纯密码登录用）")
    g.add_argument("--relogin-max", type=int, default=4,
                   help="一次运行最多自动重登录几次，默认 4（达到后停下来喊人）")
    g.add_argument("--relogin-gap", type=float, default=2.0,
                   help="两次自动登录之间的最小间隔秒数，默认 2")
    g.add_argument("--captcha-model-dir", default=None,
                   help="captcha-model 目录，默认 ../captcha-model（相对本脚本）")
    g.add_argument("--captcha-model", default=None,
                   help="指定 onnx 权重，默认 runs/w16/matcher.onnx（README 推荐）")
    g.add_argument("--captcha-min-margin", type=float, default=0.0,
                   help="识别置信度低于此值就换一张图而不是提交，默认 0（模型 99%%+ 够稳）")
    g.add_argument("--captcha-attempts", type=int, default=6,
                   help="一次登录最多换几张验证码，默认 6")
    p.add_argument("--priority", default=None,
                   help="候选教学班 ID，逗号分隔，按志愿优先级；默认取 config.json 里的 course.candidates")
    p.add_argument("--hedge", action="store_true",
                   help="首发覆盖组内前 3 个候选 —— **这已经是默认行为**，保留只为兼容")
    p.add_argument("--single", action="store_true",
                   help="首发只打第一优先级那一个班（最保守，命中率也最低）")
    p.add_argument("--write-timeout", type=float, default=4.0,
                   help="单发写请求最多占用几秒，默认 4。放课瞬间服务器过载时，挂住的"
                        "请求只会拖死它自己，不会吃掉整个窗口"
                        "（实测 15s 超时下一轮三发要 45 秒，窗口直接没了）")
    p.add_argument("--stagger", type=float, default=0.10,
                   help="首发多班之间错开多少秒，默认 0.10。实测 0.03s 会出现空回复、"
                        "0.06s 以上才稳定拿到真实业务回复")
    p.add_argument("--conns", type=int, default=1,
                   help="首发宽度（= 预热连接数），默认 1；传 2~3 等同加宽首发，硬上限 3")
    p.add_argument("--window", type=float, default=90.0,
                   help="放课后持续尝试的秒数，默认 90")
    p.add_argument("--burst", type=float, default=5.0,
                   help="放课后高强度的秒数（组内按优先级整轮轮询，不是只打第一优先级），默认 5")
    p.add_argument("--interval", type=float, default=1.0,
                   help="爆发期每轮周期（秒），默认 1.0。每轮最多连发 3 发——"
                        "实测滑动窗口约「每滚动 1 秒最多 3 发」，0.8s 安全、0.6s 会被踢")
    p.add_argument("--slow", type=float, default=1.5,
                   help="爆发期之后每轮间隔秒数，默认 1.5")
    p.add_argument("--switch-after", type=float, default=60.0,
                   help="第一组一直没名额时，多少秒后换到下一冲突组（0=不换），默认 60")
    p.add_argument("--keyword", default=None,
                   help="目标课程名（用于建白名单）；默认取 config.json 里的 course.keyword")
    p.add_argument("--force", action="store_true",
                   help="忽略单实例锁（同一账号同时只能有一个会话，跑两个会互相踢）")
    p.add_argument("--version", action="version",
                   version=f"course-grabber {school_config.__version__}")
    p.add_argument("--offline", action="store_true",
                   help="完全不联网，只打印将要发送的内容")
    return p.parse_args(argv)


LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".course-grabber.lock")


def _pid_is_our_instance(pid: int) -> bool:
    """判断锁文件里的 PID 是不是**本程序**的实例，而不是恰好复用了同一 PID 的无关进程。

    踩过的坑：容器/系统里 PID 5 往往是常驻进程，只判断"PID 存活"会让锁永远解不开，
    用户只会看到"已经有一个实例在跑"却怎么也找不到那个进程。
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # 存在但不属于我们，按存在处理
    except OSError:
        return False
    if os.name != "posix" or not os.path.isdir("/proc"):
        return True                     # 非 Linux：拿不到 cmdline，只能相信 PID
    try:
        cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "replace")
    except OSError:
        return False                    # 读不到就当它已经没了
    # 本仓库的入口与探针都可能持锁，cmdline 里认这几个关键字
    return any(k in cmd for k in ("grab", "probe_", "python"))


def acquire_instance_lock(path: str = LOCK_PATH, force: bool = False) -> bool:
    """单实例锁。

    2026-09-25 实测：学校对**同一账号**只允许一个有效会话 —— 连续登录 A→B→C，
    前一个立刻失效。所以跑两个实例（或者脚本跑着的时候去 Chrome 刷新页面）
    会互相把对方顶掉，两边都触发自动重登录、再互相顶，放课瞬间全废。
    这里用一个 PID 锁文件把"同一台机器上跑两个实例"这种情况直接拦下来。
    """
    if not force and os.path.exists(path):
        pid = 0
        try:
            with open(path, encoding="utf-8") as fh:
                pid = int((fh.read().strip() or "0"))
        except (OSError, ValueError):
            pid = 0
        # 锁是**自己**持有的不算冲突：ensure_captcha_runtime 会用 os.execve 换解释器
        # 重启脚本，PID 不变 —— 重新走一遍启动流程时会看到自己刚写的锁。
        # （只有"PID 是别人且确实是我们这个程序的实例"才拦。）
        if pid != os.getpid() and _pid_is_our_instance(pid):
            log(f"✗ 检测到已经有一个实例在跑（PID {pid}，锁文件 {path}）")
            log("  学校对同一账号只允许一个有效会话：再跑一个会把那个顶掉，")
            log("  两个进程互相踢、互相重登录，放课瞬间两边都抢不到。")
            log("  确认那个进程已经没用了再加 --force，或先 kill 掉它。")
            return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return True                      # 锁文件写不了就别拦着用户

    def _release() -> None:
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read().strip() == str(os.getpid()):
                    os.unlink(path)
        except OSError:
            pass

    atexit.register(_release)
    return True


def _chrome_cookie_into(school: School, args) -> bool:
    """读 Chrome Cookie 库并把整套 Cookie 塞进 school。失败时打印怎么办。"""
    try:
        cookie, count = cookie_header_from_chrome(args.profile)
    except Exception as exc:  # noqa: BLE001
        log(f"✗ 读取 Chrome Cookie 失败: {exc}")
        log("  提示: 写一个凭据文件即可让脚本自己登录 → "
            f"{args.credentials or (school_auth.DEFAULT_CREDENTIALS if school_auth else '')}")
        log("        或 --cookie 'name=value; name2=value2' 手动提供")
        return False
    school.use_cookie(cookie)
    info(f"已从 Chrome 读取 {count} 个 {CFG.cookie_domain} Cookie（每次运行都重新读取）")
    return True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # ---- 换解释器（本机 python3 没有 onnxruntime 时）必须在打印任何东西之前做完，
    #      否则 os.execve 会把 banner 打两遍 ----
    if not args.offline and not args.no_relogin and school_auth is not None:
        cred_path = os.path.expanduser(args.credentials or school_auth.DEFAULT_CREDENTIALS)
        if args.password or os.path.exists(cred_path):
            try:
                school_auth.ensure_captcha_runtime(
                    args.captcha_model_dir or str(school_auth.DEFAULT_MODEL_DIR))
            except school_auth.AuthError as exc:
                log(f"✗ {exc}")
                return 2

    # ---- token（有凭据时可省：登录成功后学校会发新 token）----
    token, referer = "", ""
    if args.url:
        m = re.search(r"[?&]token=([A-Za-z0-9\-]+)", args.url)
        if not m:
            log("✗ 无法从 --url 里解析出 token，请粘贴浏览器地址栏里的完整业务页链接")
            return 2
        token, referer = m.group(1), args.url

    log("=" * 72)
    log("  教务系统抢课 · 放课窗口精准首发")
    log("=" * 72)
    log(f"  会话来源   : {'--url 里的 token' if token else '自动登录（学号+密码）'}")
    if token:
        log(f"  token      : {token[:8]}…{token[-4:]}")
    log(f"  目标课程   : {args.keyword or CFG.course.get('keyword') or '（未指定，靠 --priority 指定教学班）'}")
    log(f"  首发时刻   : {'立即' if args.now else args.at + ' (北京时间)'}")
    log(f"  模式       : {'⚠ LIVE 真实提交' if args.live else '只读预检（不加 --live 不会提交）'}")

    # ---- 写接口硬性限速（实测结论，见 README 第六节）----
    # volunteer.do 在 ≈2 req/s 下 12/12 正常，到 ≈4 req/s 第 5 发就被限流，
    # 而限流会**立刻作废整个会话**。所以这里强制一个下限，任何参数组合都别想越线。
    # 实测：每轮 3 发，间隔 0.8s 安全、0.6s 会被限流。硬下限取 0.8s。
    MIN_WRITE_GAP = 0.80
    if args.interval < MIN_WRITE_GAP:
        log(f"⚠ --interval {args.interval}s 太激进（实测 0.6s 会被限流并作废会话），"
            f"已强制提升到 {MIN_WRITE_GAP}s")
        args.interval = MIN_WRITE_GAP
    if args.slow < args.interval:
        args.slow = args.interval
    # 实测：volunteer.do 的滑动窗口约「每滚动 1 秒最多 3 发」——
    # 连发 3 发没事（间隔 50ms 也行），第 4 发立刻限流并作废会话，静默多久都攒不出额度。
    if args.conns > 3:
        log(f"⚠ --conns {args.conns} 超过硬上限 3（实测第 4 发连发必被限流并作废会话），已压到 3")
        args.conns = 3

    if args.offline:
        log("\n--offline：不联网。下面是提交时会发送的请求体：")
        first_tc = (args.priority or (DEFAULT_CANDIDATES[0][0] if DEFAULT_CANDIDATES
                                     else "<教学班ID>")).split(",")[0]
        demo = {"data": {"operationType": "1", "studentCode": args.student or "<学号>",
                         "electiveBatchCode": "<批次>", "teachingClassId": first_tc,
                         "isMajor": IS_MAJOR, "campus": "<校区>",
                         "teachingClassType": CLASS_TYPE}}
        log("  POST " + PATH_VOLUNTEER)
        log("  addParam=" + json.dumps(demo, ensure_ascii=False, separators=(",", ":")))
        return 0

    # ---- 单实例锁（同一账号同时只能有一个有效会话）----
    if not acquire_instance_lock(force=args.force):
        return 2

    # ---- 凭据（学号 + 密码，用于自动登录 / 被踢重登录）----
    creds = None
    solver = None
    auth = None
    if args.no_relogin:
        info("--no-relogin：只用 Chrome/命令行 Cookie，不启用自动登录")
    elif school_auth is None:
        log(f"⚠ 读不到 school_auth.py（{_AUTH_IMPORT_ERROR}），自动登录不可用")
    else:
        try:
            creds = school_auth.load_credentials(
                args.credentials,
                args.student if args.password else None,
                args.password)
        except school_auth.AuthError as exc:
            log(f"✗ 凭据不可用: {exc}")
            return 2
        if creds is None:
            info(f"没有凭据文件（{args.credentials or school_auth.DEFAULT_CREDENTIALS}），"
                 f"自动登录不可用 —— 改用 Cookie 模式")
        else:
            try:
                solver = school_auth.CaptchaSolver(args.captcha_model_dir, args.captcha_model,
                                                min_margin=args.captcha_min_margin)
            except school_auth.AuthError as exc:
                log(f"✗ 验证码模型不可用: {exc}")
                return 2
            auth = school_auth.ReloginManager(
                creds, solver, max_logins=args.relogin_max, min_gap=args.relogin_gap,
                captcha_attempts=args.captcha_attempts, log=info)
            info(f"自动登录已就绪：学号 {creds.student_id[:4]}****{creds.student_id[-2:]}"
                 f"（凭据来源 {creds.source}）+ {solver.model.name}，"
                 f"最多 {auth.max_logins} 次重登录")

    # ---- 学号 ----
    if not token and auth is None:
        log("✗ 既没有 --url（token），也没有可用的自动登录凭据 —— 至少要有一样")
        if school_auth is not None:
            log("  " + school_auth.chromeless_note().replace("\n", "\n  "))
        return 2
    code = args.student or (creds.student_id if creds else None) or detect_student_code(args.profile)
    if not code:
        log("✗ 无法自动识别学号，请加 --student 2026xxxxxx 或写凭据文件")
        return 2
    if creds and code != creds.student_id:
        log(f"✗ --student {code} 与凭据文件里的学号 {creds.student_id} 不一致，拒绝启动")
        return 2

    # ---- 首套会话：优先「凭据文件直接登录」，其次命令行 Cookie，最后 Chrome ----
    school = School(token, "", referer)
    school.code = code
    school.auth = auth
    school.write_timeout = max(0.5, args.write_timeout)
    log(f"[{ts()}] 学号: {code[:4]}****{code[-2:]}")

    if auth is not None and not args.cookie:
        info("自动登录：正在用学号+密码建立学校会话…")
        if not school.recover("启动"):
            if auth.fatal:
                log(f"✗ {auth.fatal}")
                log("  请核对凭据文件里的学号/密码后重跑。")
                return 3
            log("✗ 自动登录失败（详见上面的 [auth] 日志）")
            if args.no_chrome:
                return 3
            log("  → 回退到 Chrome Cookie 模式")
            if not _chrome_cookie_into(school, args):
                return 2
    elif args.cookie:
        school.use_cookie(args.cookie)
        info(f"使用命令行提供的 Cookie（{len(args.cookie)} 字符）")
    elif args.no_chrome:
        log("✗ --no-chrome 且没有可用凭据：既不能自动登录，也不许读 Chrome")
        if school_auth is not None:
            log("  " + school_auth.chromeless_note().replace("\n", "\n  "))
        return 2
    elif not _chrome_cookie_into(school, args):
        return 2

    # ---- 只读预检 ----
    info("预检 1/5：校验会话（student/{code}.do）")
    ok, payload = school.probe()
    if not ok and school.auth is not None:
        info(f"      会话无效（{payload.get('msg')}），尝试自动登录恢复…")
        if school.recover("预检发现会话失效"):
            ok, payload = school.probe()
    if not ok:
        log(f"✗ 会话无效: code={payload.get('code')} msg={payload.get('msg')}")
        if school.auth is not None:
            log(f"  → 自动登录也没能救回来: {school.auth.why_not() or '见上面的 [auth] 日志'}")
        else:
            log("  → 请在 Chrome 里重新打开选课页面（刷新登录），然后重跑本脚本；")
            log("    或者写一个凭据文件让脚本自己登录；")
            log("    Cookie 每次运行都会重新从 Chrome 读取，无需手动粘贴。")
        return 3
    info(f"      会话有效（token {school.token[:8]}…{school.token[-4:]}，"
         f"本次已自动登录 {school.relogins} 次）")
    data = payload.get("data") or {}
    batch_info = data.get("electiveBatch") or {}
    batch = str(batch_info.get("code") or "")
    campus = str(data.get("campus") or "01")
    limit = str(data.get("limitElective") or "")
    info(f"      批次: {batch_info.get('name')} / {batch_info.get('typeName')} "
         f"/ {batch_info.get('tacticName')}")
    info(f"      开放: {batch_info.get('beginTime')} → {batch_info.get('endTime')}")
    info(f"      校区: {campus}   学分上下限: {limit or '未返回'}")
    if batch_info.get("typeName") and "预选" in str(batch_info.get("name") or ""):
        log("⚠ 当前批次名含“预选”：预选是抽签，抢课无意义。请确认现在是正选/复选/补选。")

    info("预检 2/5：读取已选课程（不可触碰清单）")
    try:
        enrolled = school.enrolled_ids()
    except Exception as exc:  # noqa: BLE001
        log(f"✗ 读取已选课程失败: {exc}")
        return 2
    info(f"      已选 {len(enrolled)} 个教学班，全部只读、绝不改动")
    if not enrolled:
        log("      ⚠ 已选课程列表返回 0 条。若你本来有已选课程，说明学校此刻正在重排数据")
        log("        （实测 20:00 放课前后会返回「选课系统正在初始化」），该接口暂时不可信：")
        log("        「已选中就跳过」的保护和最终复核都可能失效，请以学校页面为准。")

    info("预检 3/5：从课程目录解析候选教学班")
    try:
        catalog = school.catalog_candidates(code, batch, campus, args.keyword or str(CFG.course.get('keyword') or ''))
    except Exception as exc:  # noqa: BLE001
        log(f"⚠ 目录解析失败（不影响抢课，但白名单将只依赖 --priority）: {exc}")
        catalog = []
    if catalog:
        for c in catalog:
            log(f"      {c['tc_id']} 序{c['index']:>2} {c['teacher']:<6} "
                f"{c['place'][:26]:<26} 满={c['is_full']} 冲突={c['is_conflict']}")
        log(f"      课程号 {catalog[0]['course_number']}  学分 {catalog[0]['credit']}"
            f"（MOOC 学分不占学分上限，非 MOOC 学分才计入 limitElective 上限）")
    else:
        log("      （目录未返回结果，将只用 --priority 指定的教学班）")

    # 白名单：优先用 --priority，否则用默认优先级；目录只用于校验与展示
    # group_of: 教学班 -> 冲突组（星期-节次），用来决定哪些候选可以并发
    builtin_labels = {tc: lab for tc, lab, _g in DEFAULT_CANDIDATES}
    group_of = {tc: g for tc, _lab, g in DEFAULT_CANDIDATES}
    for c in catalog:                                 # 目录能读到就用实测课表覆盖
        g = time_group(c.get("place") or "")
        if g:
            group_of[c["tc_id"]] = g

    if args.priority:
        label_of = builtin_labels
        wanted: list[str] = []
        for item in (x.strip() for x in args.priority.split(",")):
            if not item:
                continue
            if item in label_of:                      # 完整 ID 且在内置表里
                wanted.append(item)
                continue
            hit = [tc for tc in label_of if tc.endswith(item)]
            if len(hit) == 1:                         # 允许只写后 2~3 位，如 --priority 308
                wanted.append(hit[0])
            elif len(hit) > 1:
                log(f"✗ --priority 里的 '{item}' 匹配到多个候选，请写完整 ID")
                return 2
            else:                                     # 表外的完整 ID，原样使用
                wanted.append(item)
        candidates = [(tc, label_of.get(tc, tc)) for tc in wanted]
    else:
        candidates = [(tc, lab) for tc, lab, _g in DEFAULT_CANDIDATES]

    if catalog:
        allowed = {c["tc_id"] for c in catalog}
        outside = [tc for tc, _ in candidates if tc not in allowed]
        if outside:
            log(f"⚠ 以下教学班不在本次目录白名单里，已剔除: {outside}")
            candidates = [(tc, lab) for tc, lab in candidates if tc in allowed]
    if not candidates:
        log("✗ 没有可用候选教学班，退出")
        return 2

    # 按冲突组切分，保持优先级顺序。组内互相冲突（最多中一个），组间必须串行，
    # 否则周三的班和周五的班可能同时选上 —— 这是脚本要极力避免的双选。
    groups: list[tuple[str, list[tuple[str, str]]]] = []
    for _tc, _lab in candidates:
        _g = group_of.get(_tc) or _tc        # 时段未知就各自成组（最保守）
        for _name, _members in groups:
            if _name == _g:
                _members.append((_tc, _lab))
                break
        else:
            groups.append((_g, [(_tc, _lab)]))
    if len(groups) > 1:
        for _name, _members in groups:
            info(f"  冲突组 {_name}: {[t[-3:] for t, _ in _members]}")

    # 硬保护 1：目标已在已选列表 -> 直接退出，绝不重复提交
    for tc, _lab in candidates:
        if tc in enrolled:
            log(f"\n✓ 教学班 {tc} 已在你的已选课程里 —— 无需抢课，脚本不做任何提交。")
            return 0

    info("预检 4/5：容量快照")
    for tc, lab in candidates:
        try:
            cap = school.capacity(tc, batch)
            main_txt = f"{cap.get('mainElectiveNumber')}/{cap.get('mainClassCapacity')}"
            non_txt = f"{cap.get('nonMainElectiveNumber')}/{cap.get('nonMainClassCapacity')}"
            total_i = int(cap.get("nonMainClassCapacity") or 0)
            free = total_i - int(cap.get("nonMainElectiveNumber") or 0)
            flag = ("⚠ 无数据（系统初始化中，接口不可信）" if total_i <= 0
                    else "有空位" if free > 0 else "已满")
            info(f"      {tc} {lab[:22]:<22} 主选 {main_txt:>8}  非主选 {non_txt:>6}  → {flag}")
        except Exception as exc:  # noqa: BLE001
            info(f"      {tc} 容量查询失败: {exc}")

    info("预检 5/5：对齐学校服务器时钟（HTTP Date 头区间估计）")
    offset, half_w = server_offset(school)
    if half_w == -2.0:
        info("      区间未收敛（有离群样本）—— 不猜偏移，按 0 处理并给足提前量")
        offset, half_w = 0.0, 300.0
    elif half_w < 0:
        info("      取不到 Date 头 —— 按 0 偏移处理")
        offset, half_w = 0.0, 300.0
    else:
        info(f"      服务器时钟 {offset * 1000:+.0f} ms，估计不确定度 ±{half_w:.0f} ms")
    # 宁可早一点：未开放只会被驳回，晚了就是真的没抢到。提前量至少覆盖时钟不确定度。
    # 放课是**一个瞬间**（退课位子攒到 20:00 统一放），而写接口只有 ~2 req/s，
    # 所以每一发都很贵：早打必吃「超过课容量」，等于白扔一发。
    # 因此不再强制"提前覆盖时钟不确定度"，默认就瞄准 20:00:00.000 本身。
    early = args.early_ms / 1000.0
    fire_at, when_txt = resolve_fire_time(
        args.at, offset, early, now=args.now, tomorrow=args.tomorrow)
    if early > 0:
        info(f"      提前 {early * 1000:.0f} ms 出手")
    elif early < 0:
        info(f"      推后 {-early * 1000:.0f} ms 出手（宁晚勿早）")
    else:
        info(f"      瞄准服务器 {args.at} 正点出手（时钟不确定度 ±{half_w:.0f} ms）")
    info(f"      出手时机: {when_txt}")
    if fire_at - time.time() < 0:
        fire_at = time.time()
    log("")
    log(f"  首发倒计时: {fire_at - time.time():.1f} 秒  "
         f"(本地 {datetime.fromtimestamp(fire_at, BEIJING).strftime('%H:%M:%S.%f')[:-3]} 北京)")

    if not args.live:
        log("")
        log("=" * 72)
        log("  预检全部通过。当前是【只读模式】，到点不会提交。")
        log("  要真正抢课，请加 --live 重新运行：")
        if school.auth is not None:
            log(f"    python3 {os.path.basename(__file__)} --live")
        else:
            log(f"    python3 {os.path.basename(__file__)} --url '<你的链接>' --live")
        log("=" * 72)
        if not args.now:
            _rehearsal(fire_at)
        return 0

    # ---------------- LIVE ----------------
    state = State()
    # 写请求节拍器：实测滚动 1 秒最多 3 发，第 4 发会被限流并作废整个会话。
    # 首发的裸 socket 与重试循环共用同一个节拍器，否则两边各打各的就会凑出第 4 发
    # —— 2026-09-24 20:00 和 09-25 复现实验都是这么死的。
    install_stop_handler(state)      # Ctrl-C / kill = 优雅停止（打印结果后再退出）
    pacer = WritePacer(per_window=3, window=1.0, margin=0.15, min_gap=args.stagger)
    school.pacer = pacer
    end_at = fire_at + args.window
    info(f"本次窗口：{datetime.fromtimestamp(fire_at, BEIJING).strftime('%H:%M:%S')} → "
         f"{datetime.fromtimestamp(end_at, BEIJING).strftime('%H:%M:%S')}"
         f"（--window {args.window:.0f}s），之后自动收尾退出；"
         f"中途 Ctrl-C 可随时手动停")
    info(f"写请求节拍：滚动 {pacer.window:.0f}s 内最多 {pacer.per_window} 发，"
         f"相邻两发至少隔开 {pacer.min_gap * 1000:.0f}ms（多等 {pacer.margin * 1000:.0f}ms "
         f"安全边界）—— 首发与重试共用同一份额度")
    fire_at = _refresh_before_fire(args, school, fire_at, state)

    info("预热 TCP 连接（握手提前完成，放课瞬间不再付 RTT）")
    # 首发只打第一冲突组。默认全部连接都打组内第一优先级；
    # --hedge 时组内每个候选各占一条连接，到点一起发（组内互相冲突，最多中一个）。
    top_group = groups[0][1]
    # 首发宽度：**默认就是组内前 3 个班、彼此错开 --stagger（默认 0.10s）**。
    # 放开这一步的依据是 2026-09-25 的实测：错开 0.06s 以上的 3 发写请求全部会被
    # 真正评估，而一秒钟的写额度本来就有 3 发（第 4 发才会被限流并作废会话）。
    # 只想打一个班就用 --single（最保守），--conns N 可以单独指定宽度。
    width = min(3, len(top_group))
    if args.single:
        width = 1
    if args.conns > 1:
        width = max(width, min(3, args.conns))
    width = min(width, 3, len(top_group))
    volley = [tc for tc, _lab in top_group[:width]]

    # **错开**发，而不是同时发：实测（concurrent_probe.py，3/3 复现）
    #   3 发同一瞬间出去 → 只有 1 发拿到真正的业务回复，另外 2 发是 code=2 且 msg 为空的
    #                        怪回复 —— 学校对同一学生的并发提交做了互斥，等于白扔 2 发额度，
    #                        而且拿到真回复的是哪一个班还是随机的；
    #   错开 0.06s 以上   → 3 发全部拿到真正的业务回复（0.03s 时仍会偶发空 msg）。
    # 取 0.10s：3 个班在 0.2 秒内全部被真正评估，仍在一秒钟 3 发的额度之内。
    plans: list[tuple[bytes, str, float]] = []
    for i, tc in enumerate(volley):
        plans.append((build_wire(school, code, batch, tc, campus), tc,
                      fire_at + i * args.stagger))
    info(f"  首发 {len(plans)} 发（硬上限 3）→ {[t[-3:] for t in volley]}"
         + (f"，彼此错开 {args.stagger * 1000:.0f}ms" if len(plans) > 1 else ""))

    results: list = []
    # 多发首发串成"上一发响应回来再发下一发"（最多多等 0.35s）：
    # 既不会因为并发互斥白扔后两发，也不会被一个挂住的请求拖死。
    gates = [threading.Event() for _ in range(len(plans) + 1)]
    gates[0].set()
    threads = [threading.Thread(target=sniper,
                                args=(wire, tc, at, state, results, i, pacer,
                                      min(args.write_timeout, 2.0)),
                                kwargs={"gate": gates[i], "done": gates[i + 1],
                                        "gate_timeout": 0.35},
                                daemon=True)
               for i, (wire, tc, at) in enumerate(plans)]
    for t in threads:
        t.start()

    # 首发之后的持续尝试（内部会等到 fire_at+0.15 才出手）
    worker = threading.Thread(
        target=_retry_loop,
        args=(school, code, batch, campus, groups, state, args, fire_at),
        daemon=True)
    worker.start()

    # 连接可能还要等 fire_at 才写出，join 超时必须覆盖这段等待
    # 分片 join：这样 Ctrl-C 之后能马上收尾，而不是干等满超时
    join_deadline = time.time() + max(10.0, max(at for _w, _t, at in plans) - time.time() + 25.0)
    for t in threads:
        while t.is_alive() and time.time() < join_deadline and not state.stopped():
            t.join(timeout=0.2)

    # 处理首发结果
    for idx, tc, status, body, dt in sorted(results):
        if state.is_done():
            break
        text = body.decode("utf-8", "replace") if body else ""
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {}
        verdict, msg = classify(payload, status, text)
        state.record(tc, verdict, msg)
        info(f"首发 #{idx} {tc} HTTP {status} {dt:.0f}ms → {verdict} {msg[:60]}")
        if verdict in (V_SUBMITTED, V_DUPLICATE):
            _settle(school, code, tc, state)
        elif verdict in (V_CONFLICT, V_TERMINAL):
            state.block(tc)

    # 多班首发后做一次双选检测（只报警停机，绝不自动退课）
    if len(plans) > 1:
        info("多班首发结束，检测是否发生双选…")
        _audit_parallel(school, candidates, state, enrolled)

    if not state.is_done() and not state.stopped():
        info("首发未定，交给重试循环…")
        stop_by = time.time() + args.window + 30
        while worker.is_alive() and time.time() < stop_by and not state.stopped():
            worker.join(timeout=0.25)

    return _report(state, candidates, school.relogins, school.pacer)


def _rehearsal(fire_at: float) -> None:
    """只读演练：跑到 T-3s 就停，报告计时精度，不提交。"""
    log("")
    info("计时演练：将自旋到 T-3s 并测量调度精度（不提交任何请求）")
    while True:
        left = fire_at - time.time()
        if left <= 3.0:
            break
        time.sleep(min(1.0, left - 3.0))
    t0 = time.perf_counter()
    target = time.perf_counter() + (fire_at - time.time())
    spins = 0
    while time.perf_counter() < target:
        spins += 1
    err = (time.perf_counter() - target) * 1000
    info(f"      自旋 {spins} 圈，到点误差 {err:+.2f} ms —— 这就是首发的时间精度")


def _refresh_before_fire(args, school: School, fire_at: float,
                         state: "State | None" = None) -> float:
    """T-30s 重新拿一次会话并复验（Chrome Cookie 可能已刷新，会话也可能刚好过期）。

    这一步很关键：等到 19:59:30 才发现会话死了，还有 30 秒可以自动重登录，
    而不用等到放课瞬间才发现。
    """
    left = fire_at - time.time()
    if left > 35:
        if school.auth is not None:
            info(f"等待放课（{left / 60:.1f} 分钟）… T-30s 会重新取会话并复验")
        else:
            info(f"等待放课（{left / 60:.1f} 分钟）… T-30s 会重新读取 Cookie 并复验会话")
        while fire_at - time.time() > 32 and not (state and state.stopped()):
            _sleep(min(5.0, fire_at - time.time() - 32), state, step=0.5)

    if school.auth is None and not args.cookie:
        try:
            new_cookie, count = cookie_header_from_chrome(args.profile)
            if new_cookie != school.cookie:
                info(f"T-30s：Chrome Cookie 已更新（{count} 个），换用最新值")
                school.use_cookie(new_cookie)
            else:
                info("T-30s：Chrome Cookie 未变化")
        except Exception as exc:  # noqa: BLE001
            info(f"T-30s：重新读取 Cookie 失败，沿用旧值: {exc}")

    ok, payload = school.probe()
    if not ok and school.auth is not None:
        info(f"T-30s：会话已失效（{payload.get('msg')}），立刻自动重登录…")
        if school.recover("T-30s 复验失败"):
            ok, _payload = school.probe()
    if ok:
        info(f"T-30s：会话复验通过（token {school.token[:8]}…{school.token[-4:]}），连接就绪")
    else:
        info(f"⚠ T-30s 会话复验失败: {payload.get('msg')}"
             f"{'（自动登录也没救回来）' if school.auth else '（请在 Chrome 重新登录）'}")
    return fire_at


def _fire_round(school: School, code: str, batch: str, campus: str,
                targets: list[str], timeout: float | None = None) -> dict[str, tuple]:
    """一轮最多 3 个班：各自独立短连接，前一发**响应回来**（或等满 0.35s）再发下一发。

    两条约束同时满足：
      * 不被挂住的请求拖死 —— 每发最多等 `overlap_wait` 秒就先发下一发，
        不会像单连接顺序发那样，一个超时把整轮拖成 3×timeout（15s 年代是 45 秒）；
      * 不撞学校的并发互斥 —— 实测同一学生的写请求若上一发还在处理，
        新的一发会拿到 `code=2` 且 msg 为空的空回复，等于白打（服务端处理 239ms 时，
        固定 100ms 间隔的三发里有两发就是这样）。等响应回来再发就没有这个问题。
    """
    out: dict[str, tuple] = {}
    lock = threading.Lock()

    def one(tc: str) -> None:
        try:
            res = school.submit_fresh(code, batch, tc, campus, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 单发失败不能拖累同轮其它候选
            res = ({"error": str(exc)}, 0, str(exc), 0.0)
        with lock:
            out[tc] = res

    threads: list[threading.Thread] = []
    for tc in targets:
        th = threading.Thread(target=one, args=(tc,), daemon=True)
        th.start()
        threads.append(th)
        # 关键：等这一发的**响应**回来再发下一个（最多等 overlap_wait 秒）。
        # 学校对同一个学生的写请求有并发互斥 —— 上一发还在处理时打进来的请求会拿到
        # `code=2` 且 msg 为空的空回复，那一发等于没打（实测：服务端处理 239ms 时，
        # 固定间隔 100ms 的三发里有两发就是这种空回复）。
        # 等响应就不会撞上互斥；而 overlap_wait 上限保证服务器挂住时也只耽误 0.35 秒，
        # 不会像单连接顺序发那样被一个超时吃掉整个窗口。
        th.join(timeout=0.35)

    budget = (timeout or school.write_timeout) + 5.0
    for th in threads:
        th.join(timeout=budget)
    return out


def _retry_loop(school: School, code: str, batch: str, campus: str,
                groups: list, state: State, args, fire_at: float) -> None:
    """首发之后的持续尝试。

    按冲突组串行：先把第一组打穿（全被拒或中了一个），才轮到下一组。
    绝不跨组并发/交替提交 —— 否则周三的班和周五的班可能同时选上，变成双选。

    组内每一轮按优先级顺序走一遍（而不是死磕第一优先级）：
    06 这种低命中率的班放第一时，只打它会把放课后最关键的几秒全押空。
    放课后前 --burst 秒用 --interval 的快节奏，之后放慢到 --slow 秒一轮。

    第一组若一直"满员"（不是被拒，只是没名额），它不会自己让位，
    所以超过 --switch-after 秒仍无进展时强制换到下一组 —— 换过去就不再回头，
    这也是安全的：一旦在下一组中了就停，不会又回到上一组造成双选。
    """
    # 首发已经用掉几发额度，重试循环要让开一个窗口。
    # 真正的硬保证在 WritePacer 里（首发与这里共用同一份"每秒 3 发"额度）；
    # 这个 start 只是让日志与节奏好看，并且避免第一轮一开始就撞在节拍器上干等。
    start = fire_at + args.interval
    while time.time() < start and not state.is_done() and not state.stopped():
        time.sleep(min(0.05, max(0.0, start - time.time())))
    burst_until = fire_at + args.burst
    switch_at = fire_at + args.switch_after
    deadline = fire_at + args.window
    # 系统初始化期间谁提交都没用，这段停机不该算进"第一组多少秒没名额就换组"的计时
    outage_since = 0.0
    sent = 0
    last_write = 0.0              # 上一发写请求的发出时刻（用来保证最小间隔）
    pace = args.interval          # 自适应节奏：被限流就退避，顺利就回到初始值

    for gi, (gname, members) in enumerate(groups):
        if state.is_done() or state.stopped():
            break
        pool = [(tc, lab) for tc, lab in members if tc not in state.blocked_ids()]
        if not pool:
            continue
        info(f"  进入冲突组 {gname}: {[t[-3:] for t, _ in pool]}")
        while time.time() < deadline and not state.is_done() and not state.stopped():
            pool = [(tc, lab) for tc, lab in members if tc not in state.blocked_ids()]
            if not pool:
                info(f"  冲突组 {gname} 全部被拒，换下一组")
                break
            if (gi < len(groups) - 1 and args.switch_after > 0 and time.time() > switch_at
                    and outage_since <= 0):
                info(f"  冲突组 {gname} 过了 {args.switch_after:.0f}s 仍没名额，轮到下一组")
                break
            in_burst = time.time() < burst_until
            base_gap = args.interval if in_burst else max(args.slow, args.interval)
            round_gap = max(base_gap, pace)   # 被限流过就按退避后的节奏走
            # 每轮最多 3 个候选（滑动窗口「每滚动 1 秒最多 3 发」），
            # 组内按优先级取前 3 个：不把爆发期全押在命中率最低的第一优先级上。
            t_round = time.time()

            # 先挑出本轮要打的候选。爆发期直接用提交当探针（抢的就是那几百毫秒）；
            # 之后就先用只读的 capacity.do 探一下，没空位就不发写请求 ——
            # 这样脚本可以整晚挂着捡漏，而不会把账号打成风控。
            picks: list[tuple[str, str]] = []
            for tc, lab in pool:
                if len(picks) >= 3:
                    break
                if not in_burst and has_slot(school, tc, batch) is False:
                    continue
                picks.append((tc, lab))
            if outage_since > 0 and picks:
                # 服务端在初始化：多打没意义（每发都会被拒），但必须保持试探而且要快 ——
                # 恢复的那一瞬间才是位子真正可抢的时刻。停机期间改成每 0.6 秒打 1 发：
                # 反应快一倍，写请求反而更少。
                picks = picks[:1]
                round_gap = 0.6
            if not picks:
                _sleep(round_gap - (time.time() - t_round), state)
                continue

            # 爆发期的写超时压到 2 秒：服务器过载时挂住的请求必须尽快放弃，
            # 否则"等超时"本身就吃掉了窗口。正常的写请求 RTT 是 60~200ms，
            # 2 秒足够宽松；万一真被误弃，那一发在服务端仍可能落库，
            # 复核时 courseResult 里会看到，重复提交也只会回"已经选过"。
            wt = min(args.write_timeout, 2.0) if in_burst else args.write_timeout

            # 爆发期并发错开发（谁也拖不死谁）；爆发期之后回归一连接顺序发 ——
            # 那时是"挂着捡漏"，慢一点无所谓，串行还能把双选的可能性再压一档。
            if in_burst and len(picks) > 1:
                res = _fire_round(school, code, batch, campus,
                                  [tc for tc, _lab in picks], timeout=wt)
            else:
                res = {}
                for tc, _lab in picks:
                    try:
                        res[tc] = school.submit(code, batch, tc, campus, timeout=wt)
                    except Exception as exc:  # noqa: BLE001 - 单发异常不拖累其它候选
                        res[tc] = ({"error": str(exc)}, 0, str(exc), 0.0)

            for tc, lab in picks:
                if state.is_done() or state.stopped():
                    break
                payload, status, text, dt = res.get(tc, ({}, 0, "", 0.0))
                sent += 1
                last_write = time.time()
                verdict, msg = classify(payload, status, text)
                state.record(tc, verdict, msg)
                if verdict == V_BUSY:
                    # 学校说"请求过快"就别硬顶，否则会被踢掉会话
                    # 退避上限在爆发期压到 1.5s：被限流要收敛，但放课后这几秒
                    # 不能一路退到几秒一发，否则等于放弃窗口。
                    pace = min(pace * 2.0, 1.5)
                    if outage_since <= 0:
                        outage_since = time.time()
                        info("  ⚠ 服务端进入初始化/限流状态（这段时间不计入换组倒计时）")
                    info(f"  被限流/初始化中（{msg[:30]}），退避到 {pace * 1000:.0f}ms")
                elif verdict == V_OVERLOAD:
                    # 服务器过载（5xx / 超时 / HTML 错误页）：放课窗口就那么几秒，
                    # 不能像被限流那样一路退避，保持窗口节奏继续打。
                    info(f"  服务器过载（{msg[:34]}），保持节奏继续")
                elif verdict in (V_FULL, V_WINDOW, V_UNKNOWN):
                    pace = max(args.interval, pace * 0.7)
                    if outage_since > 0:
                        back = time.time() - outage_since
                        switch_at += back
                        info(f"  ✓ 服务恢复（停机 {back:.0f}s，已从换组倒计时里扣除）")
                        outage_since = 0.0
                if verdict in (V_SUBMITTED, V_DUPLICATE):
                    info(f"  {tc} → {verdict} ({dt:.0f}ms) {msg[:50]}")
                    _settle(school, code, tc, state)
                    if state.is_done():
                        info(f"  本轮共提交 {sent} 次")
                        return
                elif verdict in (V_CONFLICT, V_TERMINAL):
                    info(f"  {tc} {lab[:20]} → {verdict}: {msg[:50]}  移出候选")
                    state.block(tc)
                elif verdict == V_EXPIRED:
                    # 单发判过期不能信：限流伪装的过期会让脚本在放课瞬间自杀。
                    info(f"  疑似过期（{msg[:30]}），用只读接口复核会话…")
                    if not session_dead(school, code):
                        info("  会话其实还活着（刚才那发是限流造成的假过期），继续")
                        pace = min(pace * 2.0, 1.5)
                        continue
                    # 真死了：以前这里直接终止整个任务（2026-09-24 就是死在这一步，
                    # 比放课早 350ms 自杀）。现在先自动登回来 —— 被限流踢掉是常态，
                    # 而放课窗口只有几十秒，等人来救等于放弃。
                    if school.auth is not None and school.recover("被学校踢掉会话"):
                        pace = args.interval          # 新会话，节奏从头开始
                        continue
                    log("")
                    log("!" * 72)
                    log("  ✗ 会话确实已失效（多半是被限流踢掉，或学校在放课时重排了数据）")
                    if school.auth is not None:
                        log(f"  自动重登录也没能救回来：{school.auth.why_not() or '见上面 [auth] 日志'}")
                    log("  立刻在 Chrome 重新打开选课页面登录，然后原样重跑本脚本 ——")
                    log("  Cookie 会自动重新读取，且过了 20:00 也会立刻开打，不必等明天。")
                    log("!" * 72)
                    with state.lock:
                        state.done = True
                    return
            if not state.is_done() and not state.stopped():
                # 按"整轮周期"睡，扣掉本轮已经花掉的时间 —— 目标就是每秒一轮、每轮 3 发。
                _sleep(round_gap - (time.time() - t_round), state)
    if state.stopped():
        info(f"  已手动停止，本轮共提交 {sent} 次")
    else:
        info(f"  本轮共提交 {sent} 次")


def _audit_parallel(school: School, candidates: list, state: State,
                    before: set[str]) -> list[str]:
    """并行首发后的双选检测。

    并发的两个冲突请求有可能同时通过学校的冲突检查再各自落库（TOCTOU 竞态）。
    这里只检测、只报警、只停机 —— 绝不自动退课，退课必须由人来做决定。
    """
    try:
        now_ids = school.enrolled_ids()
    except Exception as exc:  # noqa: BLE001
        info(f"  双选检测跳过（已选列表读取失败）: {exc}")
        return []
    got = [tc for tc, _ in candidates if tc in now_ids and tc not in before]
    if len(got) > 1:
        log("")
        log("!" * 72)
        log("  ⚠ 检测到并行首发同时选上了同一门课的多个教学班：")
        for tc in got:
            log(f"      {tc}  {dict(candidates).get(tc, '')}")
        log("  脚本不会替你退课。请自己打开学校页面，退掉你不要的那个。")
        log("  注意：退课后再选可能触发学校的「退选再选」限制，请一次想清楚。")
        log("!" * 72)
        with state.lock:
            state.done = True
            state.multi = got
        return got
    if len(got) == 1:
        info(f"✓✓✓ 已由学校已选课程列表确认选中: {got[0]}  {dict(candidates).get(got[0], '')}")
        state.finish(got[0])
    return got


def _settle(school: School, code: str, tc_id: str, state: State) -> None:
    """学校受理后：异步状态 + 已选课程列表双重复核。"""
    info(f"  学校已受理 {tc_id}，开始严格复核…")
    try:
        st = process_ok(school, code)
        info(f"  studentstatus: {st}")
    except Exception as exc:  # noqa: BLE001
        info(f"  studentstatus 异常: {exc}")
    for attempt in (1, 2):
        try:
            if confirm(school, tc_id):
                info(f"✓✓✓ 已由学校已选课程列表确认选中: {tc_id}")
                state.finish(tc_id)
                return
            info(f"  ⚠ 已选列表暂未出现 {tc_id}，继续尝试")
            return
        except SessionExpired:
            if attempt == 1 and school.auth is not None and school.recover("复核时会话过期"):
                info("  会话已恢复，重新复核…")
                continue
            log("✗ 复核时会话过期且无法自动恢复，请刷新 Chrome 登录后重跑（或修好凭据文件）")
            with state.lock:
                state.done = True
            return


def _report(state: State, candidates: list, relogins: int = 0,
            pacer: "WritePacer | None" = None) -> int:
    log("")
    log("=" * 72)
    labels = dict(candidates)
    if state.stopped():
        log("  （本次是你手动停下的：没抢到不代表窗口结束，随时可以原样重跑）")
    if relogins:
        log(f"  本次运行自动重登录 {relogins} 次（被学校限流踢掉后自行恢复）")
    if pacer is not None:
        log(f"  写请求节拍器：共放行 {pacer.total} 发，其中 {pacer.waits} 次为守住"
            f"「每秒 {pacer.per_window} 发」而等待")
    if state.multi:
        log("  结果：⚠ 并行首发同时选上了多个教学班，需要你手动退掉多余的：")
        for tc in state.multi:
            log(f"      {tc}  {labels.get(tc, '')}")
        log("  脚本没有、也不会替你退课。请在 20:00 后尽快自行处理。")
        log("=" * 72)
        return 4
    if state.confirmed:
        log(f"  结果：抢到 {state.confirmed}  {labels.get(state.confirmed, '')}")
        log("  请立刻去学校页面核对；“已选课程”才是最终依据。")
        log("=" * 72)
        return 0
    log("  结果：本次窗口内未确认抢到。")
    log("  已选课程列表里没有出现目标教学班 —— 可能就是没抢上，不是脚本出错。")
    log("  下一次放课可原样重跑；Cookie / 会话都会自动重新获取。")
    log("=" * 72)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n已手动中止。")
        sys.exit(130)
    except BrokenPipeError:
        # `grab.py --offline | head` 这种用法会把管道提前关掉。
        # 这是用户主动的行为，不该变成一个刺眼的 traceback。
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
