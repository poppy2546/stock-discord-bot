import os
import shutil
import subprocess
import sys
import time

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["PIP_NO_CACHE_DIR"] = "1"


def clean_container_storage(working_dir: str):
    """ล้างไฟล์แคชขยะ (pip cache, yfinance cache, __pycache__) เพื่อประหยัดพื้นที่ Storage บน bot-hosting.net"""
    freed_bytes = 0
    cache_Targets = [
        os.path.expanduser("~/.cache/pip"),
        os.path.expanduser("~/.cache/py-yfinance"),
        os.path.expanduser("~/.local/share/py-yfinance"),
        "/home/container/.cache/pip",
        "/home/container/.cache/py-yfinance",
        os.path.join(working_dir, "__pycache__"),
        os.path.join(working_dir, "tests", "__pycache__"),
        os.path.join(working_dir, "deploy"),
    ]
    for path in cache_Targets:
        if os.path.exists(path):
            try:
                for root, _, files in os.walk(path):
                    for f in files:
                        try:
                            freed_bytes += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
    if freed_bytes > 0:
        print(f"🧹 [Storage Cleaner] ล้างไฟล์แคชขยะเรียบร้อย (คืนพื้นที่ {freed_bytes / (1024 * 1024):.1f} MB)", flush=True)


def run_supervisor():
    working_dir = os.path.dirname(os.path.abspath(__file__))
    os.environ["RUNNING_UNDER_SUPERVISOR"] = "1"

    print("🛡️ [Stock Bot Supervisor] Process supervisor active. Auto-restart & 6-hour recycle enabled.", flush=True)

    while True:
        clean_container_storage(working_dir)
        print("🚀 [Stock Bot Supervisor] Launching Stock Advisor Bot...", flush=True)
        # ดึงโค้ดล่าสุดจาก Git แบบไม่ค้างรอถามรหัสผ่าน (GIT_TERMINAL_PROMPT=0)
        try:
            git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            subprocess.run(["git", "fetch", "origin", "main"], cwd=working_dir, env=git_env, timeout=10, capture_output=True)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=working_dir, env=git_env, timeout=5, capture_output=True)
        except Exception:
            pass

        start_time = time.time()
        process = subprocess.run([sys.executable, "-B", "discord_bot.py"], cwd=working_dir)
        elapsed = time.time() - start_time

        print(f"🔄 [Stock Bot Supervisor] Bot process finished (code {process.returncode}, ran for {elapsed:.1f}s).", flush=True)
        print("⏳ Restarting clean process in 2 seconds...", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    run_supervisor()
