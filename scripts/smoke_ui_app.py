"""主窗口集成冒烟：驱动真实入口 ``python -m glof.ui --smoke`` 并核验产物。

运行（项目根目录，cmd）::

    .venv\\Scripts\\python.exe scripts\\smoke_ui_app.py

冒烟本体在 ``glof/ui/__main__.py`` 的 ``--smoke`` 分支（这样冒烟走的就是
用户真实运行的那条入口路径，装配代码没有"测试专用旁路"）。本脚本负责：

* 用当前解释器子进程运行 ``python -m glof.ui --smoke``（cwd=项目根，
  环境变量 offscreen/software 由入口自己按 --smoke 设置）；
* 断言退出码 0、截图 output/ui_smoke/app.png 存在且非空；
* 转发子进程全部输出（断言明细都在那里），全绿打印 ``APP SMOKE PASS``。

素材与预期数字的诚实性说明见 ``glof/ui/__main__.py`` 模块 docstring：
默认影像在契约参数 (0.15, 0.005) 下提取 0 条是实测真实结果，冒烟按
"0 条也正常走完全链路"断言，再用 threshold=0.05 与 data/spike 景拿到
记录驱动联动。全程本地素材，不联网（时序检索用空年份区间离线验证）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PNG = ROOT / "output" / "ui_smoke" / "app.png"


def main() -> int:
    cmd = [sys.executable, "-m", "glof.ui", "--smoke"]
    print(f"$ {' '.join(cmd)}   (cwd={ROOT})", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    elapsed = time.perf_counter() - t0

    # 转发子进程输出，断言明细全在这里
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    if proc.stderr:
        print("--- 子进程 stderr ---", file=sys.stderr)
        print(proc.stderr, file=sys.stderr, flush=True)

    size = PNG.stat().st_size if PNG.exists() else 0
    ok = proc.returncode == 0
    print(f"\n子进程退出码 {proc.returncode}，耗时 {elapsed:.1f}s；"
          f"截图 {PNG.relative_to(ROOT)} {size} bytes", flush=True)

    if not ok:
        print("APP SMOKE FAIL: 子进程非零退出", file=sys.stderr)
        return 1
    if size <= 0:
        print("APP SMOKE FAIL: 截图缺失或为空", file=sys.stderr)
        return 1
    print("APP SMOKE PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
