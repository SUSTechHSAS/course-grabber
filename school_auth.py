#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动登录 / 被踢重登录（学号 + 密码 → 有效会话）

对接的是常见的"账号密码 + 点选验证码"教务登录页，流程与前端 JS 逐字段一致：

    1. POST {vcode_token}?timestamp=<ms>
       不带任何 Cookie            → {"code":"1","data":{"token":"<vtoken>"}}

    2. GET  {vcode_image}?vtoken=<vtoken>
       不带任何 Cookie            → JPEG 点选验证码
                                    + Set-Cookie: 本轮验证码 Cookie

    3. 本地识别（click-captcha-matcher）→ 4 个点击坐标，按提示顺序
       提交格式 "x-y,x-y,x-y,x-y"（与前端点击结果序列化格式一致）

    4. POST {login}
       Cookie: **只带本轮**的验证码 Cookie（绝不能混入旧会话的登录 Cookie ——
               同名 Cookie 重复时服务端取值顺序不可预测，会把登录打死）
       Body:   loginPwd   = base64(DES3(密码, 配置里的密钥))
               loginName  = 学号
               vtoken     = 第 1 步的 token
               verifyCode = 第 3 步的坐标串
       → code=1 成功，data.token 就是之后 `token:` 头要用的那一串，
                 同时下发新的登录 Cookie

返回码语义（前端 JS 原文）:
    1 = 成功          2 = 登录名或密码不正确   3 = 验证码不正确
    4 = 在线人数超过上限                     其他 = msg 里给原因

**防锁号策略**：只有 code=2 会被当成"密码错"，一旦出现就立刻放弃本次运行并且
永不再试 —— 密码错、账号被锁都要人来处理，脚本绝不硬猜。验证码错(code=3)、
在线人数超限(code=4)、网络抖动都只换一张图/退避后重试。

依赖：
    * 配置      —— school_config.py（域名、路径、Cookie 名、密码加密密钥）
    * 密码加密  —— 本目录的 `desencode.py` / `cus_base64.py`（第三方 MIT 实现，
                   见 LICENSE）
    * 验证码识别 —— click-captcha-matcher-rs（`python/solver.py` + `libccm.so`）；
                    模型编在库里，零第三方依赖
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import school_config

CFG = school_config.load()

HOST = CFG.host
PORT = CFG.port
BASE = CFG.base_url
APP = CFG.base_path
PAGE = CFG.page_path
UA = str(CFG.http.get("user_agent"))

PATH_VCODE = CFG.path("vcode_token")
PATH_VCIMAGE = CFG.path("vcode_image")
PATH_LOGIN = CFG.path("login")

DEFAULT_CREDENTIALS = str(CFG.raw.get("credentials_path")
                          or "~/.config/course-grabber/credentials.json")
HERE = school_config.HERE          # 打包后是"可执行文件所在目录"，不是临时解包目录
# 识别模型的目录，优先级：配置里指定的（存在才用）→ 打包进来的 → 同级仓库
_cfg_model_dir = Path(str(CFG.captcha.get("model_dir") or "../click-captcha-matcher-rs/python"))
if not _cfg_model_dir.is_absolute():
    _cfg_model_dir = (HERE / _cfg_model_dir).resolve()
_bundled = school_config.bundled_captcha_dir()
if _bundled is not None and not (_cfg_model_dir / "solver.py").exists():
    DEFAULT_MODEL_DIR = _bundled
else:
    DEFAULT_MODEL_DIR = _cfg_model_dir
CAPTCHA_WIDTH = int(CFG.captcha.get("width") or 250)
CAPTCHA_HEIGHT = int(CFG.captcha.get("height") or 80)
WRONG_CAPTCHA = "验证码不正确"
WRONG_PASSWORD_HINTS = ("登录名或密码不正确", "密码不正确", "用户名或密码")
ONLINE_LIMIT_HINTS = ("在线人数超过上限", "在线人数已达上限")


class AuthError(RuntimeError):
    """自动登录失败（可重试的、不可重试的都归这里，靠子类区分）。"""


class BadCredentials(AuthError):
    """学号或密码不正确 —— 绝不重试，必须人工核对。"""


class CaptchaRejected(AuthError):
    """验证码被拒（识别错误或 token 过期）—— 换一张图重试。"""


class LoginUnavailable(AuthError):
    """网络/协议/在线人数上限等临时问题 —— 退避后重试。"""


class AuthServiceDown(LoginUnavailable):
    """认证服务本身不可用（实测放课瞬间会返回 `#E2140600091 认证失败`）。

    这不是凭据或流程有问题，而是服务端在重排数据。它很短暂（实测约 20 秒），
    所以不该消耗自动重登录的次数配额，也不该让脚本以为"救不回来了"而收工。
    """


# ==========================================================================
# 一、凭据（默认路径见 config.json 的 credentials_path，建议 0600）
# ==========================================================================
@dataclass(frozen=True)
class Credentials:
    student_id: str
    password: str
    source: str

    def __repr__(self) -> str:                      # 绝不把密码写进日志
        return f"Credentials(student_id={self.student_id!r}, password=***, source={self.source!r})"

    __str__ = __repr__


def _pick(d: dict, names: tuple[str, ...]) -> str:
    for n in names:
        v = d.get(n)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


def load_credentials(path: str | os.PathLike | None = None,
                     student_id: str | None = None,
                     password: str | None = None) -> Credentials | None:
    """按「命令行参数 → 凭据文件」的顺序取学号密码；都没有则返回 None。

    命令行只作为临时覆盖：正常用法是把密码放进 0600 的凭据文件，
    这样密码不进 shell 历史、也不出现在 `ps` 里。
    """
    if student_id and password:
        return Credentials(str(student_id).strip(), str(password), "命令行")
    if password and not student_id:
        raise AuthError("给了 --password 但没给 --student（学号无法可靠地自动推断）")
    if student_id and not password:
        raise AuthError("给了 --student 但没给 --password")

    p = Path(path or DEFAULT_CREDENTIALS).expanduser()
    if not p.exists():
        return None

    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        print(f"⚠ {p} 权限是 {mode:o}，同机其他用户可能读到你的密码；"
              f"建议 chmod 600 {p}", flush=True)

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"凭据文件读取失败 {p}: {exc}") from exc
    if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
        raw = raw["credentials"]            # 容忍 {"credentials": {...}} 包一层
    if not isinstance(raw, dict):
        raise AuthError(f"凭据文件格式不对（应为 JSON 对象）: {p}")

    sid = _pick(raw, ("student_id", "studentId", "student", "code", "username", "loginName", "学号"))
    pwd = _pick(raw, ("password", "passwd", "pwd", "密码"))
    if not sid or not pwd:
        raise AuthError(f"凭据文件里缺少 student_id / password: {p}")
    if not re.fullmatch(r"\d{6,12}", sid):
        raise AuthError(f"凭据文件里的学号不像学号: {sid!r}")
    return Credentials(sid, pwd, str(p))


# ==========================================================================
# 二、密码加密（学校前端协议：DES3 + base64）
# ==========================================================================
def encrypt_password(password: str) -> str:
    """明文密码 → loginPwd。与前端 `$.base64.encode(strEnc(pwd, ...))` 等价。"""
    if not isinstance(password, str) or not password:
        raise AuthError("密码为空")
    from cus_base64 import CustomBase64
    from desencode import str_enc

    return CustomBase64().encode(str_enc(password, *CFG.des_keys))


# ==========================================================================
# 三、验证码：抓图（学校接口）+ 识别（click-captcha-matcher）
# ==========================================================================
def _set_cookie_values(set_cookie: list[str], names: tuple[str, ...]) -> str:
    """从 Set-Cookie 里取出指定名字，保持 names 的顺序，不破坏 Expires 里的逗号。"""
    text = ", ".join(set_cookie)
    vals: dict[str, str] = {}
    for m in re.finditer(r"(?:^|[,;]\s*)([A-Za-z_][A-Za-z0-9_]*)=([^;,]+)", text):
        vals[m.group(1)] = m.group(2).strip()
    return "; ".join(f"{n}={vals[n]}" for n in names if n in vals)


class _Http:
    """一次一连接的极简 HTTP 客户端（登录只用十几次请求，不需要连接池）。"""

    @staticmethod
    def request(method: str, path: str, body: bytes | None = None, cookie: str = "",
                referer: str = "", timeout: float = 15.0) -> tuple[int, bytes, list[str]]:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
        headers = {
            "Host": HOST,
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"http://{HOST}{referer or PAGE}",
            "X-Requested-With": "XMLHttpRequest",
        }
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if cookie:
            headers["Cookie"] = cookie
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read(), resp.headers.get_all("Set-Cookie") or []
        finally:
            conn.close()


@dataclass
class CaptchaChallenge:
    vtoken: str
    cookie: str          # route + insert_cookie，只属于这一张图
    image: bytes


def fetch_captcha(timeout: float = 15.0) -> CaptchaChallenge:
    """抓一张新验证码。不带任何 Cookie —— 这是登录页的原始契约。"""
    status, body, _ = _Http.request(
        "POST", f"{PATH_VCODE}?timestamp={int(time.time() * 1000)}", b"", timeout=timeout)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise LoginUnavailable(f"vcode.do 返回非 JSON（HTTP {status}）: {body[:80]!r}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    vtoken = str((data or {}).get("token") or "").strip()
    if str(payload.get("code")) != "1" or not vtoken:
        raise LoginUnavailable(f"vcode.do 未返回 token: code={payload.get('code')} "
                               f"msg={payload.get('msg')}")

    status, image, set_cookie = _Http.request(
        "GET", f"{PATH_VCIMAGE}?vtoken={urllib.parse.quote(vtoken)}", timeout=timeout)
    if status != 200 or not image.startswith(b"\xff\xd8\xff"):
        raise LoginUnavailable(f"验证码图片异常: HTTP {status} {len(image)}B")
    cookie = _set_cookie_values(set_cookie, ("route", "insert_cookie"))
    if not cookie:
        raise LoginUnavailable("验证码响应里没有 验证码 Cookie")
    return CaptchaChallenge(vtoken, cookie, image)


def _load_solver_module(directory: Path):
    """按**目录**加载 solver.py，不复用 sys.modules 里的同名模块。

    实测坑：直接 `import solver` 会命中模块缓存 —— 先指向 Rust 版目录加载过一次，
    之后即使把 model_dir 指到 onnx 版目录，拿到的还是 Rust 版那个模块。
    打包版（模块在 PYZ 里、磁盘上没有 solver.py）才回落到普通 import。
    """
    import importlib.util

    path = directory / "solver.py"
    if path.exists():
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        key = "_ccm_solver_" + str(abs(hash(str(directory))))
        if key in sys.modules:
            return sys.modules[key]
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            sys.modules.pop(key, None)
            return None
        return module
    try:
        import solver                      # 打包进去的模块
        return solver
    except ImportError:
        return None


class CaptchaSolver:
    """验证码识别：JPEG 字节 → 4 个点击坐标 + margin。

    用的是 click-captcha-matcher-rs 的 `libccm`（模型和推理一起编进约 200KB 的
    动态库），所以**不需要 onnxruntime / numpy / Pillow** —— 单文件包因此从
    39MB 降到 8.6MB，识别本身还快一倍。这是唯一后端，没有回退：留着回退路径
    打包工具照样会把那几个依赖收进去，体积一点省不下来。
    """

    def __init__(self, model_dir: str | os.PathLike | None = None,
                 model: str | os.PathLike | None = None,
                 min_margin: float = 0.0, threads: int = 1) -> None:
        self.min_margin = min_margin
        self.backend = "native(libccm)"
        self.dir = Path(model_dir or DEFAULT_MODEL_DIR).expanduser().resolve()
        module = _load_solver_module(self.dir)
        if module is None or not hasattr(module, "load_library"):
            raise AuthError(
                f"找不到可用的识别库: {self.dir}\n"
                f"  这里应该有 click-captcha-matcher-rs 的 python/solver.py 与 libccm。\n"
                f"  先构建它：cd <该仓库> && cargo build --release\n"
                f"  或用 --captcha-model-dir 指向正确目录。")
        try:
            self._solver = module.CaptchaSolver(str(model) if model else None)
            self.model = Path(module.find_library())
        except Exception as exc:  # noqa: BLE001
            raise AuthError(f"识别库加载失败: {exc}") from exc

    def solve(self, image: bytes) -> tuple[list[list[int]], float]:
        """返回 ([[x, y]] * 4, margin)。格式非法一律当识别失败抛 CaptchaRejected。"""
        try:
            points, margin = self._solver.solve(image)
        except ValueError as exc:                     # 图片尺寸不对
            raise CaptchaRejected(f"验证码图片无法解析: {exc}") from exc
        if len(points) != 4:
            raise CaptchaRejected(f"识别出的点击数是 {len(points)}，不是 4")
        out: list[list[int]] = []
        for p in points:
            x, y = int(round(p[0])), int(round(p[1]))
            if not (0 <= x <= CAPTCHA_WIDTH and 0 <= y <= CAPTCHA_HEIGHT):
                raise CaptchaRejected(f"点击坐标越界: {x},{y}")
            out.append([x, y])
        if len({tuple(p) for p in out}) != 4:
            raise CaptchaRejected("识别出的 4 个点有重合")
        return out, float(margin)

    @staticmethod
    def to_verify_code(points: list[list[int]]) -> str:
        """前端 verifyResult 的序列化格式：left-top,left-top,..."""
        return ",".join(f"{x}-{y}" for x, y in points)


# ==========================================================================
# 四、登录
# ==========================================================================
@dataclass
class LoginSession:
    token: str
    cookie: str          # 登录 Cookie + 本轮验证码 Cookie 拼起来的整串
    name: str = ""
    number: str = ""

    def referer(self, token: str | None = None) -> str:
        """之后所有业务接口要带的 Referer。

        必须是**完整 URL**：实测只给相对路径会被服务端判
        「Illegal refferer. 此页面不能跨域访问」，登录成功但每个接口都调不通。
        """
        return f"http://{HOST}{APP}/*default/grablessons.do?token={token or self.token}"


class Login:
    """一次登录会话的完整流程，带「验证码换图重试 / 密码错立刻收手」的策略。"""

    def __init__(self, creds: Credentials, solver: CaptchaSolver, *,
                 captcha_attempts: int = 6, gap: float = 0.4,
                 timeout: float = 15.0, log=print) -> None:
        self.creds = creds
        self.solver = solver
        self.captcha_attempts = max(1, int(captcha_attempts))
        self.gap = gap
        self.timeout = timeout
        self.log = log
        self._login_pwd = encrypt_password(creds.password)
        self.stats = {"images": 0, "low_margin": 0, "captcha_rejected": 0}

    def login(self) -> LoginSession:
        """抓到能过的验证码并登录成功为止。密码错 → 抛 BadCredentials（不会重试）。"""
        last: Exception | None = None
        for attempt in range(1, self.captcha_attempts + 1):
            if attempt > 1:
                time.sleep(self.gap)
            try:
                challenge = fetch_captcha(self.timeout)
            except LoginUnavailable as exc:
                last = exc
                self.log(f"[auth]   取验证码失败（{attempt}/{self.captcha_attempts}）: {exc}")
                time.sleep(min(2.0, 0.5 * attempt))
                continue
            self.stats["images"] += 1
            try:
                points, margin = self.solver.solve(challenge.image)
            except CaptchaRejected as exc:
                last = exc
                self.log(f"[auth]   识别失败（{attempt}/{self.captcha_attempts}）: {exc}")
                continue
            if margin < self.solver.min_margin:
                self.stats["low_margin"] += 1
                last = CaptchaRejected(f"置信度不足 margin={margin:.3f}")
                self.log(f"[auth]   识别把握不大 margin={margin:.3f}，换一张图")
                continue
            verify = CaptchaSolver.to_verify_code(points)
            self.log(f"[auth]   验证码 {verify}  margin={margin:.3f}  "
                     f"（第 {attempt}/{self.captcha_attempts} 张）")
            try:
                return self._submit(challenge, verify)
            except CaptchaRejected as exc:
                self.stats["captcha_rejected"] += 1
                last = exc
                self.log(f"[auth]   学校说验证码不对（{attempt}/{self.captcha_attempts}），换一张")
        if isinstance(last, LoginUnavailable):
            raise LoginUnavailable(
                f"连续 {self.captcha_attempts} 次都没能取到可用验证码: {last}")
        raise CaptchaRejected(f"连续 {self.captcha_attempts} 张验证码都没过: {last}")

    def _submit(self, challenge: CaptchaChallenge, verify: str) -> LoginSession:
        form = {
            "loginPwd": self._login_pwd,
            "loginName": self.creds.student_id,
            "vtoken": challenge.vtoken,
            "verifyCode": verify,
        }
        try:
            status, body, set_cookie = _Http.request(
                "POST", PATH_LOGIN, urllib.parse.urlencode(form).encode("utf-8"),
                cookie=challenge.cookie, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - 网络问题当临时故障
            raise LoginUnavailable(f"login.do 请求失败: {exc}") from exc

        try:
            payload = json.loads(body)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        code = str(payload.get("code") or "")
        msg = str(payload.get("msg") or "").strip()
        hay = f"{msg} {body[:200].decode('utf-8', 'replace')}"

        if code == "1":
            data = payload.get("data") or {}
            token = str(data.get("token") or "").strip()
            login_cookie = _set_cookie_values(set_cookie, CFG.session_cookies)
            if not token or not login_cookie:
                raise LoginUnavailable(
                    f"登录成功但响应缺少会话信息: token={bool(token)} cookie={bool(login_cookie)}")
            cookie = "; ".join(x for x in (login_cookie, challenge.cookie) if x)
            return LoginSession(token=token, cookie=cookie,
                                name=str(data.get("name") or ""),
                                number=str(data.get("number") or self.creds.student_id))

        if code == "2" or any(w in hay for w in WRONG_PASSWORD_HINTS):
            raise BadCredentials(f"学号或密码不正确（HTTP {status}）: {msg or hay[:80]}")
        if code == "3" or WRONG_CAPTCHA in hay:
            raise CaptchaRejected(msg or "验证码不正确")
        if "认证失败" in hay or code.startswith("#"):
            raise AuthServiceDown(f"认证服务暂时不可用（code={code}）: {msg or hay[:60]}")
        if code == "4" or any(w in hay for w in ONLINE_LIMIT_HINTS):
            raise LoginUnavailable(f"在线人数超过上限，稍后再试: {msg}")
        raise LoginUnavailable(f"登录被拒（HTTP {status} code={code}）: {msg or hay[:80]}")


# ==========================================================================
# 五、带次数上限 / 冷却的自动重登录（给抢课主流程用）
# ==========================================================================
class ReloginManager:
    """管住"被踢了就自动登回来"这件事的次数与节奏。

    刻意保守：一次运行最多 `max_logins` 次自动登录，两次之间至少 `cooldown` 秒，
    一旦发现密码错（BadCredentials）就永久熔断 —— 后端连续失败可能触发风控/锁号。
    """

    def __init__(self, creds: Credentials, solver: CaptchaSolver, *,
                 max_logins: int = 4, cooldown: float = 3.0,
                 captcha_attempts: int = 6, min_gap: float = 2.0, log=print) -> None:
        self.creds = creds
        self.solver = solver
        self.max_logins = max(1, int(max_logins))
        self.cooldown = max(0.0, float(cooldown))
        self.captcha_attempts = captcha_attempts
        self.min_gap = max(0.0, float(min_gap))
        self.log = log
        self.attempts = 0
        self.last_at = 0.0
        self.fatal: str | None = None          # 一旦设置，之后所有 relogin 直接失败
        self.history: list[tuple[float, bool, str]] = []

    @property
    def available(self) -> bool:
        return self.fatal is None and self.attempts < self.max_logins

    def why_not(self) -> str:
        if self.fatal:
            return self.fatal
        if self.attempts >= self.max_logins:
            return f"本次运行已自动登录 {self.attempts} 次，达到上限（--relogin-max）"
        return ""

    def relogin(self, reason: str = "") -> LoginSession | None:
        """尽力登回来。成功返回新会话，失败返回 None（原因写在日志里）。"""
        if not self.available:
            self.log(f"[auth] 放弃自动重登录: {self.why_not()}")
            return None
        wait = self.min_gap - (time.time() - self.last_at)
        if wait > 0:
            time.sleep(wait)
        self.attempts += 1
        self.last_at = time.time()
        what = "启动登录" if reason == "启动" else f"第 {self.attempts}/{self.max_logins} 次自动重登录"
        tag = f"（{reason}）" if reason and reason != "启动" else ""
        self.log(f"[auth] {what}{tag} …")
        t0 = time.time()
        try:
            session = Login(self.creds, self.solver,
                            captcha_attempts=self.captcha_attempts, log=self.log).login()
        except BadCredentials as exc:
            self.fatal = f"学号或密码不正确，已熔断自动重登录: {exc}"
            self.history.append((time.time(), False, str(exc)))
            self.log(f"[auth] ✗ {self.fatal}")
            self.log("[auth]   请核对凭据文件；脚本不会再用错误密码重试（避免账号被锁）。")
            return None
        except AuthServiceDown as exc:
            # 服务端认证自己在重排数据 —— 不消耗次数配额，下一轮再试
            self.attempts -= 1
            self.last_at = 0.0
            self.history.append((time.time(), False, str(exc)))
            self.log(f"[auth] ⚠ {exc}")
            self.log("[auth]   这是服务端的问题（放课瞬间常见），不消耗重登录次数，稍后自动重试")
            return None
        except AuthError as exc:
            self.history.append((time.time(), False, str(exc)))
            self.log(f"[auth] ✗ 自动重登录失败（{time.time() - t0:.1f}s）: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001
            self.history.append((time.time(), False, repr(exc)))
            self.log(f"[auth] ✗ 自动重登录异常: {exc}")
            return None
        self.history.append((time.time(), True, session.name))
        self.log(f"[auth] ✓ 登录成功（{time.time() - t0:.1f}s）"
                 f"{'  ' + session.name if session.name else ''}")
        if self.cooldown:
            time.sleep(min(self.cooldown, 1.0))   # 让新会话先落稳
        return session


def chromeless_note() -> str:
    """给"没有凭据也没 Chrome"时的提示文案。"""
    return ("没有可用凭据，也没读到 Chrome Cookie。\n"
            f"  写一个 {DEFAULT_CREDENTIALS}（chmod 600）：\n"
            '    {"student_id": "2026xxxxxx", "password": "你的密码"}\n'
            "  或临时用 --student XXX --password XXX 传一次。")


def which_python() -> str:
    return shutil.which("python3") or sys.executable
