#!/usr/bin/env python3
"""
doubao2API BrowserOnly Gateway 启动脚本

后端: uvicorn          http://localhost:7861  (API 网关)
"""
import os
import sys
import subprocess
import time
import signal
from pathlib import Path

WORKSPACE_DIR = Path(__file__).parent.absolute()
BACKEND_DIR = WORKSPACE_DIR / "backend"
LOGS_DIR = WORKSPACE_DIR / "logs"
DATA_DIR = WORKSPACE_DIR / "data"


def ensure_dirs():
    LOGS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)


def check_python():
    if sys.version_info < (3, 10):
        print("❌ 需要 Python 3.10+，当前版本:", sys.version)
        sys.exit(1)


def install_backend_deps():
    print("⚡ [1/3] 安装后端依赖...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
            cwd=BACKEND_DIR,
            env=env,
        )
        print("✓ 后端依赖已就绪")
    except Exception as e:
        print(f"⚠ 后端依赖安装异常: {e}")


def install_playwright():
    print("⚡ [2/3] 安装 Playwright Chromium 浏览器...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        if result.returncode == 0:
            print("✓ Chromium 已安装，跳过")
            return
    except Exception:
        pass
    print("  -> 正在下载 Chromium（首次运行，请耐心等待）...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            cwd=WORKSPACE_DIR,
            env=env,
        )
        print("✓ Chromium 下载完成")
    except Exception as e:
        print(f"⚠ Chromium 下载异常: {e}")


def kill_port(port: int):
    """终止占用指定端口的进程"""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                        print(f"  -> 已终止占用 {port} 端口的旧进程 (PID: {pid})")
                        time.sleep(1)
                        return
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"],
                capture_output=True, text=True, timeout=5,
            )
            pid = result.stdout.strip()
            if pid:
                subprocess.run(["kill", "-9", pid], capture_output=True)
                print(f"  -> 已终止占用 {port} 端口的旧进程 (PID: {pid})")
                time.sleep(1)
    except Exception:
        pass


def start_backend() -> subprocess.Popen:
    print("⚡ [3/3] 启动后端服务...")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(WORKSPACE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    port = env.get("PORT", "7861")
    kill_port(int(port))

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "backend.main:app",
            "--host", "0.0.0.0",
            "--port", port,
            "--workers", "1",
        ],
        cwd=WORKSPACE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    print(f"✓ 后端进程已启动 (PID: {proc.pid})，正在初始化浏览器引擎...")

    import threading
    ready_event = threading.Event()

    def read_output():
        for line in iter(proc.stdout.readline, b""):
            try:
                decoded = line.decode("utf-8", errors="replace")
            except Exception:
                decoded = str(line)
            print(decoded, end="")
            if "Browser engine started" in decoded or "Application startup complete" in decoded:
                ready_event.set()

    threading.Thread(target=read_output, daemon=True).start()

    started = ready_event.wait(timeout=300)
    if not started:
        print("⚠ 后端初始化超时，服务可能未完全就绪")
    else:
        print("✓ 服务已完全就绪")

    return proc


def main():
    ensure_dirs()
    check_python()
    install_backend_deps()
    install_playwright()
    backend_proc = start_backend()

    port = os.environ.get("PORT", "7861")
    print()
    print("=" * 50)
    print("  doubao2API BrowserOnly Gateway 已上线")
    print(f"  后端 API:     http://127.0.0.1:{port}")
    print(f"  API 文档:     http://127.0.0.1:{port}/docs")
    print("=" * 50)
    print("  按 Ctrl+C 停止服务")
    print()

    def signal_handler(sig, frame):
        print("\n正在关闭服务...")
        try:
            backend_proc.terminate()
        except Exception:
            pass
        backend_proc.wait()
        print("服务已停止")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while True:
            if backend_proc.poll() is not None:
                print(f"❌ 后端进程异常退出 (Exit Code: {backend_proc.returncode})")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if backend_proc.poll() is None:
                backend_proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
