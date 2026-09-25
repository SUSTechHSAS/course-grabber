#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行配置：所有"学校相关"的东西都在这里读进来，代码里只留占位符。

为什么要有这一层
----------------
这套脚本对接的是**某一个**学校的选课系统：域名、接口路径、Cookie 名、密码加密的
密钥、验证码版面、候选教学班……这些全都带着那个学校的指纹。把它们写死在代码里，
仓库就不再是"一套抢课工具"，而是"某个学校的抢课工具"。

所以这里把它们全部外置：

    cp config.example.json config.json     # 填上你自己学校的那套值
    # config.json 已在 .gitignore 里，不会被提交

读配置的顺序（先找到的先用）：
    1. 环境变量 COURSE_GRABBER_CONFIG 指向的文件
    2. 当前目录下的 config.json
    3. ~/.config/course-grabber/config.json

代码里只保留**与学校无关**的默认值：超时、限流节奏、并发形状这些工程参数。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

__version__ = "1.0.0"

ENV_VAR = "COURSE_GRABBER_CONFIG"
APP_DIR = "~/.config/course-grabber"

# PyInstaller 打包后：脚本目录在临时解包目录里，配置要认"可执行文件旁边"。
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    HERE = Path(sys.executable).resolve().parent      # 用户看到的目录
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    HERE = BUNDLE_DIR

# 顺带把打包进来的模块目录挂上（验证码识别那几个模块在这里）
if FROZEN:
    sys.path.insert(0, str(BUNDLE_DIR))


# --help / --version 这类"还没配置也要能用"的调用：任何模块在此时读配置都走宽松模式
_HELPISH = any(a in ("-h", "--help", "--version") for a in sys.argv[1:])


def _search_paths() -> list[str]:
    """配置文件的搜索顺序：环境变量 → 程序旁边 → 当前目录 → 用户配置目录。"""
    return [
        os.environ.get(ENV_VAR) or "",
        str(HERE / "config.json"),
        str(Path.cwd() / "config.json"),
        os.path.expanduser(f"{APP_DIR}/config.json"),
    ]

# 与学校无关的工程默认值。config.json 里没写的键就用这里的。
ENGINE_DEFAULTS: dict = {
    "timezone_offset_hours": 8,        # 放课时刻按这个时区解释
    "http": {
        "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        "read_timeout": 15.0,          # 只读请求超时
        "write_timeout": 4.0,          # 写请求超时（爆发期会自动压到 2s）
        "connect_timeout": 1.5,        # 首发建连超时（失败会重试到点）
    },
    "pacing": {
        "per_window": 3,               # 滚动窗口内最多几发写请求（实测值，按自己学校改）
        "window": 1.0,
        "margin": 0.15,                # 安全边界，别贴着窗口边界发
        "min_gap": 0.10,               # 相邻两发最小间隔（避开"同一学生并发提交"互斥）
        "overlap_wait": 0.35,          # 多发时最多等上一发响应多久
    },
    "credentials_path": f"{APP_DIR}/credentials.json",
    "captcha": {
        "model_dir": "../click-captcha-matcher",
        "model": "runs/w16/matcher.onnx",
        "width": 250,
        "height": 80,
        "min_margin": 0.0,
    },
    "course": {
        "keyword": "",
        "class_type": "",
        "campus": "",
        "candidates": [],
    },
}


class ConfigError(RuntimeError):
    """配置缺失或格式不对。"""


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """读进来的配置。属性访问 + `path(name)` 取接口路径。"""

    def __init__(self, raw: dict, source: str, lenient: bool = False) -> None:
        self.lenient = lenient
        self.raw = raw
        self.source = source
        for key, value in raw.items():
            setattr(self, key, value)
        self.school = raw.get("school") or {}
        self.paths = raw.get("paths") or {}
        self.http = raw.get("http") or {}
        self.pacing = raw.get("pacing") or {}
        self.captcha = raw.get("captcha") or {}
        self.course = raw.get("course") or {}
        self.cookies = raw.get("cookies") or {}
        self.password = raw.get("password") or {}

        missing = [k for k in ("host",) if not self.school.get(k)]
        if not self.paths:
            missing.append("paths")
        if missing and not lenient:
            raise ConfigError(
                f"配置不完整（缺 {', '.join(missing)}）: {source}\n"
                f"  照 config.example.json 填一份 config.json 再跑。")

        self.base_url = f"http://{self.host}" + (f":{self.port}" if self.port not in (80, None) else "")

    # ---- 常用取值 ----
    @property
    def host(self) -> str:
        return str(self.school.get("host") or "")

    @property
    def port(self) -> int:
        return int(self.school.get("port") or 80)

    @property
    def cookie_domain(self) -> str:
        return str(self.school.get("cookie_domain") or self.host)

    @property
    def base_path(self) -> str:
        return str(self.school.get("base_path") or "").rstrip("/")

    @property
    def login_page(self) -> str:
        """登录页路径（有些系统登录后会带着 token 跳到某个业务页）。"""
        return str(self.school.get("login_page") or self.school.get("page_path") or "/")

    @property
    def page_path(self) -> str:
        return str(self.school.get("page_path") or "/")

    @property
    def des_keys(self) -> list[str]:
        keys = self.password.get("des_keys")
        if not keys:
            if self.lenient:
                return []
            raise ConfigError(f"配置里缺少 password.des_keys（密码加密用的密钥）: {self.source}")
        return [str(k) for k in keys]

    @property
    def session_cookies(self) -> tuple[str, ...]:
        return tuple(self.cookies.get("session") or ("JSESSIONID",))

    @property
    def captcha_cookies(self) -> tuple[str, ...]:
        return tuple(self.cookies.get("captcha") or ())

    @property
    def expect_cookies(self) -> tuple[str, ...]:
        """登录后应该出现的 Cookie 名，缺了就打警告（不阻断）。"""
        return tuple(self.cookies.get("expect") or ())

    @property
    def candidates(self) -> list[tuple[str, str, str]]:
        """候选教学班：[(教学班ID, 展示名, 冲突组)]，顺序即志愿优先级。"""
        out = []
        for item in self.course.get("candidates") or []:
            if isinstance(item, dict):
                out.append((str(item.get("id", "")), str(item.get("label", "")),
                            str(item.get("group", ""))))
            else:
                out.append(tuple(str(x) for x in item))    # 允许直接写三元组
        return [(i, lab, g) for i, lab, g in out if i]

    def path(self, name: str, **fmt: object) -> str:
        """取接口路径；`{base}` 会替换成 base_path，其余 `{x}` 用实参填。"""
        raw = self.paths.get(name)
        if not raw:
            if self.lenient:            # --help/--version：用不到真实路径
                return f"/{name}"
            raise ConfigError(f"配置里缺少 paths.{name}: {self.source}")
        text = str(raw).replace("{base}", self.base_path)
        return text.format(**fmt) if fmt else text.replace("{code}", "{code}")


_CACHE: Config | None = None


def load(path: str | os.PathLike | None = None, *, refresh: bool = False,
         optional: bool | None = None) -> Config:
    """读配置（带缓存）。

    `optional=True` 用于 `--help` / `--version` 这类"还没配置也要能用"的场景：
    找不到配置就返回一份空配置，不报错、也不生成文件。不传时自动按命令行判断。
    """
    if optional is None:
        optional = _HELPISH
    global _CACHE
    if _CACHE is not None and not refresh and path is None:
        return _CACHE

    cands = [str(path)] if path else [p for p in _search_paths() if p]
    for cand in cands:
        p = Path(cand).expanduser()
        if p.is_file():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise ConfigError(f"配置文件解析失败 {p}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"配置文件应当是一个 JSON 对象: {p}")
            cfg = Config(_deep_merge(ENGINE_DEFAULTS, raw), str(p))
            if path is None:
                _CACHE = cfg
            return cfg

    if optional:
        return Config(_deep_merge(ENGINE_DEFAULTS, {}), "<未配置>", lenient=True)

    # 一条都没有：如果旁边有模板，就替用户生成一份，并明确告诉他下一步做什么。
    target = HERE / "config.json"
    example = HERE / "config.example.json"
    if not path and example.is_file() and not target.exists():
        try:
            shutil.copyfile(example, target)
        except OSError:
            pass
        else:
            raise ConfigError(
                f"第一次运行：已按模板生成配置文件\n    {target}\n"
                "请填上你学校的域名、接口路径与候选教学班，然后重新运行。")
    raise ConfigError(
        "找不到配置文件。先照着模板填一份：\n"
        f"    cp {example} {target}\n"
        f"    # 然后编辑 {target}，填上你学校的域名与接口路径\n"
        f"也可以放到 {APP_DIR}/config.json，或用环境变量 {ENV_VAR} 指定路径。")


def example_path() -> Path:
    return HERE / "config.example.json"


def bundled_captcha_dir() -> Path | None:
    """打包进来的识别模型目录（PyInstaller 的 _MEIPASS/captcha）。

    代码模块（solver/geometry/assign）是被打进 PYZ 的，所以这里可能看不到 solver.py；
    只要权重目录在，就说明是打包版。
    """
    cand = BUNDLE_DIR / "captcha"
    if (cand / "solver.py").exists() or (cand / "runs").exists():
        return cand
    return None
