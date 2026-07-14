#!/usr/bin/env python3

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    venv_dir = root / ".venv"
    python_bin = venv_dir / "bin" / "python"

    if not venv_dir.exists():
        run([sys.executable, "-m", "venv", str(venv_dir)], root)

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = f"{venv_dir / 'bin'}:{env.get('PATH', '')}"

    print("+", f"{python_bin} -m pip install --upgrade pip")
    subprocess.run(
        [str(python_bin), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=str(root),
        check=True,
        env=env,
    )

    print("+", f"{python_bin} -m pip install -r requirements.txt")
    subprocess.run(
        [str(python_bin), "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=str(root),
        check=True,
        env=env,
    )

    print()
    print("Environment ready.")
    print(f"Start Web console:  {root / 'run.sh'} --console")
    print(f"Or run recorder:    {root / 'scripts' / 'run_recorder.sh'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
