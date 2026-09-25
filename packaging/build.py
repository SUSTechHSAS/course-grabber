#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把项目打包成"下载即用"的单文件可执行程序（PyInstaller）。

产物结构（以 Linux 为例）：

    dist/course-grabber-linux-x64/
        course-grabber           ← 单文件可执行程序（含识别模型与 onnxruntime）
        config.example.json      ← 配置模板
        run.sh                   ← 第一次运行帮你生成 config.json 并提示怎么填
        README.md

用法:

    python3 packaging/build.py                     # 自动找 ../click-captcha-matcher
    CAPTCHA_MODEL_REPO=/path/to/repo python3 packaging/build.py
    python3 packaging/build.py --onedir            # 目录版：启动快，但不是单文件

需要一个装了 pyinstaller 的解释器；onnxruntime / pillow / numpy 会在打包识别模型时用到：

    uv venv .build-venv && uv pip install --python .build-venv/bin/python \\
        pyinstaller onnxruntime pillow numpy
    .build-venv/bin/python packaging/build.py
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "course-grabber"

# 识别库（Rust 版）：python/solver.py 是 ctypes 封装，libccm 里编着模型。
# 两者加起来约 250KB —— 所以整包能压到 8~9MB，且不再需要 onnxruntime/numpy/Pillow。
MATCHER_REPO_DIRNAME = "click-captcha-matcher-rs"
LIB_NAMES = ("libccm.so", "libccm.dylib", "ccm.dll")


def find_matcher_repo(explicit: str | None) -> Path | None:
    """找到 Rust 版识别库仓库（需要 python/solver.py 和已编译的 libccm）。"""
    cands = [explicit, os.environ.get("CAPTCHA_MODEL_REPO"),
             os.environ.get("CCM_REPO"),
             str(ROOT.parent / MATCHER_REPO_DIRNAME),
             str(ROOT / MATCHER_REPO_DIRNAME)]
    for cand in cands:
        if not cand:
            continue
        base = Path(cand).expanduser()
        py_dir = base / "python" if (base / "python" / "solver.py").is_file() else base
        if not (py_dir / "solver.py").is_file():
            continue
        lib = find_lib(base)
        if lib is None:
            continue
        return base
    return None


def find_lib(base: Path) -> Path | None:
    """定位已编译的 libccm（cargo build --release 的产物）。"""
    cands = [base / "target" / "release" / n for n in LIB_NAMES]
    cands += [base / n for n in LIB_NAMES]
    cands += [base / "python" / n for n in LIB_NAMES]
    for c in cands:
        if c.is_file():
            return c
    return None


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def stage_matcher(repo: Path, dest: Path) -> Path:
    """把 solver.py 与 libccm 复制到暂存目录（PyInstaller 从这里取）。"""
    dest.mkdir(parents=True, exist_ok=True)
    py_dir = repo / "python" if (repo / "python" / "solver.py").is_file() else repo
    shutil.copyfile(py_dir / "solver.py", dest / "solver.py")
    lib = find_lib(repo)
    shutil.copyfile(lib, dest / lib.name)
    return lib


def platform_tag() -> str:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
        platform.system(), platform.system().lower())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine)
    return f"{system}-{arch}"


def archive(src_dir: Path, tag: str) -> Path:
    if tag.startswith("windows"):
        out = src_dir.with_suffix(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src_dir.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(src_dir.parent))
    else:
        out = src_dir.with_suffix(".tar.gz")
        with tarfile.open(out, "w:gz") as tf:
            tf.add(src_dir, arcname=src_dir.name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onedir", action="store_true", help="打成目录而不是单文件")
    ap.add_argument("--model-repo", default=None, help="识别模型仓库路径")
    ap.add_argument("--keep-build", action="store_true", help="保留 PyInstaller 中间目录")
    args = ap.parse_args()

    tag = platform_tag()
    build_dir = ROOT / "build"
    stage = build_dir / "captcha"
    dist = ROOT / "dist"

    if build_dir.exists():
        shutil.rmtree(build_dir)
    stage.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--name", NAME,
           "--onedir" if args.onedir else "--onefile",
           "--distpath", str(dist), "--workpath", str(build_dir / "pyi"),
           "--specpath", str(build_dir)]

    # 只保留真正用得到的部分。实测这些排除项能砍掉一大半体积：
    #   onnxruntime.transformers/quantization/tools 会连带拉进 sympy、onnx 等一堆东西，
    #   我们只调 InferenceSession；ssl/readline/tkinter/unittest 这套标准库也用不到。
    for mod in ("tkinter", "unittest", "pydoc", "doctest", "test", "distutils",
                "setuptools", "pip", "readline", "ssl", "sympy", "onnx",
                "matplotlib", "scipy", "pandas", "IPython", "pytest",
                # 识别改成 libccm 之后，这三个彻底不需要了（以前它们占 33MB）
                "onnxruntime", "numpy", "PIL"):
        cmd += ["--exclude-module", mod]

    repo = find_matcher_repo(args.model_repo)
    if repo is None:
        print(f"✗ 找不到识别库仓库 {MATCHER_REPO_DIRNAME}（需要 python/solver.py 与已编译的 libccm）。\n"
              "  先构建它：cd ../click-captcha-matcher-rs && cargo build --release\n"
              "  或用 CAPTCHA_MODEL_REPO=/path/to/repo 指定。")
        return 2
    lib = stage_matcher(repo, stage)
    size_kb = lib.stat().st_size / 1024
    print(f"识别库: {lib}  ({size_kb:.0f} KB，模型已编在里面)")
    cmd += ["--paths", str(stage),
            "--hidden-import", "solver",
            "--add-binary", f"{stage / lib.name}{os.pathsep}."]

    if platform.system() != "Windows":
        cmd.append("--strip")

    cmd.append(str(ROOT / "grab.py"))
    run(cmd, cwd=str(ROOT))

    # ---- 组装发布包 ----
    exe_name = f"{NAME}.exe" if platform.system() == "Windows" else NAME
    out_dir = dist / f"{NAME}-{tag}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    if args.onedir:
        shutil.copytree(dist / NAME, out_dir / NAME, dirs_exist_ok=True)
    else:
        shutil.copyfile(dist / exe_name, out_dir / exe_name)
        (out_dir / exe_name).chmod(0o755)

    shutil.copyfile(ROOT / "config.example.json", out_dir / "config.example.json")
    shutil.copyfile(ROOT / "README.md", out_dir / "README.md")
    launcher = "run.bat" if platform.system() == "Windows" else "run.sh"
    shutil.copyfile(ROOT / "packaging" / launcher, out_dir / launcher)
    (out_dir / launcher).chmod(0o755)

    pkg = archive(out_dir, tag)
    if not args.keep_build:
        shutil.rmtree(build_dir, ignore_errors=True)

    size_mb = pkg.stat().st_size / 1024 / 1024
    print(f"\n✅ 产物: {pkg}  ({size_mb:.1f} MB)")
    print(f"   解包后目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
