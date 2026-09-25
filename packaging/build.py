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
    python3 packaging/build.py --no-bundle-model   # 不打包识别模型（纯抢课逻辑，体积小很多）
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

# 识别模型仓库里真正需要在运行时用到的东西（训练脚本不打包）
MODEL_MODULES = ("solver.py", "geometry.py", "assign.py")
MODEL_WEIGHT = "runs/w16/matcher.onnx"


def find_model_repo(explicit: str | None) -> Path | None:
    cands = [explicit, os.environ.get("CAPTCHA_MODEL_REPO"),
             str(ROOT.parent / "click-captcha-matcher"),
             str(ROOT / "click-captcha-matcher")]
    for cand in cands:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if (p / "solver.py").is_file() and (p / MODEL_WEIGHT).is_file():
            return p.resolve()
    return None


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def stage_model(repo: Path, dest: Path) -> None:
    """把识别模块与权重摆成运行时的目录结构（PyInstaller 打进去的就是这个）。"""
    dest.mkdir(parents=True, exist_ok=True)
    for name in MODEL_MODULES:
        shutil.copyfile(repo / name, dest / name)
    weight_dst = dest / Path(MODEL_WEIGHT).parent
    weight_dst.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / MODEL_WEIGHT, weight_dst / Path(MODEL_WEIGHT).name)


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
    ap.add_argument("--no-bundle-model", action="store_true",
                    help="不打包验证码识别模型（体积小很多，但自动登录不可用）")
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
                "onnxruntime.transformers", "onnxruntime.quantization",
                "onnxruntime.tools", "onnxruntime.training"):
        cmd += ["--exclude-module", mod]

    if not args.no_bundle_model:
        repo = find_model_repo(args.model_repo)
        if repo is None:
            print("✗ 找不到识别模型仓库（click-captcha-matcher）。\n"
                  "  用 CAPTCHA_MODEL_REPO=/path/to/repo 指定，或加 --no-bundle-model。")
            return 2
        print(f"识别模型仓库: {repo}")
        stage_model(repo, stage)
        # 让 PyInstaller 能分析到 solver/geometry/assign（numpy、PIL 会被连带收进来）
        cmd += ["--paths", str(stage),
                "--hidden-import", "solver",
                "--hidden-import", "geometry",
                "--hidden-import", "assign",
                "--hidden-import", "onnxruntime",
                "--add-data", f"{stage / 'runs'}{os.pathsep}captcha/runs"]
        cmd += ["--hidden-import", "PIL.Image", "--hidden-import", "numpy"]

    if args.no_bundle_model:
        cmd += ["--exclude-module", "onnxruntime", "--exclude-module", "numpy",
                "--exclude-module", "PIL"]

    if platform.system() != "Windows":
        cmd.append("--strip")

    cmd.append(str(ROOT / "grab.py"))
    run(cmd, cwd=str(ROOT))

    # ---- 组装发布包 ----
    exe_name = f"{NAME}.exe" if platform.system() == "Windows" else NAME
    # 轻量版单独命名：核心只有 8MB 左右，不含验证码识别
    pkg_name = f"{NAME}-lite" if args.no_bundle_model else NAME
    out_dir = dist / f"{pkg_name}-{tag}"
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
