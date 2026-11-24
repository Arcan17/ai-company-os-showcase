"""Issue #4 — no QA/automation artefact may create, move, resize, focus or close windows of the user's personal Safari (or drive the GUI at all)."""
import os
import re
import stat
import subprocess
import sys
import threading
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FORBIDDEN = re.compile(r"osascript|safaridriver|screencapture|System Events|tell application\s+\\?\"?Safari|make new document|set bounds|close \(every window|AXPress|\bkeystroke\b|key code|do JavaScript|activate\b.*Safari|NSAppleScript", re.I)
SCAN_DIRS = ["qa", "backend/app", "frontend/e2e", "frontend/src"]
SCAN_FILES = ["run_demo_comercial.sh", "run_demo.sh", "run_v05_lab.sh", "frontend/playwright.config.ts", "frontend/playwright.audit.config.ts", "frontend/playwright.demo.config.ts", "frontend/vite.config.ts"]
SELF = os.path.abspath(__file__)


def files():
    out = [os.path.join(ROOT, f) for f in SCAN_FILES if os.path.exists(os.path.join(ROOT, f))]
    for d in SCAN_DIRS:
        for base, _, names in os.walk(os.path.join(ROOT, d)):
            if "node_modules" in base or "__pycache__" in base:
                continue
            out += [os.path.join(base, n) for n in names if n.endswith((".py", ".sh", ".ts", ".tsx", ".js", ".mjs", ".json"))]
    return [f for f in out if os.path.abspath(f) != SELF]


def test_no_script_or_test_can_drive_safari_or_the_gui():
    bad = []
    for f in files():
        for i, line in enumerate(open(f, encoding="utf-8", errors="ignore"), 1):
            if FORBIDDEN.search(line):
                bad.append(f"{os.path.relpath(f, ROOT)}:{i}: {line.strip()[:100]}")
    assert not bad, "GUI/Safari automation found:\n" + "\n".join(bad)


def test_the_retired_applescript_script_is_gone_and_replaced_by_a_manual_checklist():
    assert not os.path.exists(os.path.join(ROOT, "qa", "safari_check.py"))
    assert os.path.exists(os.path.join(ROOT, "qa", "safari_manual_checklist.py"))


def test_playwright_never_launches_the_users_safari():
    for cfg in ("playwright.config.ts", "playwright.audit.config.ts", "playwright.demo.config.ts"):
        t = open(os.path.join(ROOT, "frontend", cfg)).read()
        assert "Safari" not in t and "channel:" not in t and "chromium_headless_shell" in t, cfg   # an explicit headless Chromium binary only


def test_no_launcher_opens_a_browser_unless_explicitly_opted_in():
    demo = open(os.path.join(ROOT, "run_demo_comercial.sh")).read()
    assert not re.search(r"^\s*[^#\n]*\bopen\s+\"?http", demo, re.M) and "--open" not in demo.split("set -euo pipefail")[1].split("CMD=")[0]
    lab = open(os.path.join(ROOT, "run_demo.sh")).read()
    for line in lab.splitlines():
        if re.search(r"\bopen\s+\"?http", line) and not line.lstrip().startswith("#"):
            assert "AICOS_OPEN_BROWSER" in line, line   # the lab launcher opens a browser only with AICOS_OPEN_BROWSER=1


def test_demo_test_command_does_not_touch_safari_or_enable_automation():
    sh = open(os.path.join(ROOT, "run_demo_comercial.sh")).read()
    block = re.search(r"\n  test\)(.*?)\n    ;;", sh, re.S).group(1)
    assert "safari" not in block.lower() and "osascript" not in block and "defaults write" not in block and "--enable" not in block
    assert "playwright.demo.config.ts" in block and "test_demo.py" in block
    assert "defaults write" not in sh and "safaridriver" not in sh


def test_checklist_runs_without_invoking_any_gui_tool(tmp_path):
    """Shim every GUI-capable binary on PATH: if the checklist (or anything it spawns) calls one, the shim records it and the test fails."""
    log = tmp_path / "called.log"
    for name in ("osascript", "open", "safaridriver", "screencapture", "defaults", "killall", "cliclick"):
        p = tmp_path / name
        p.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {log}\nexit 99\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "qa", "safari_manual_checklist.py"), "1"], capture_output=True, text=True, env=env, timeout=60)   # port 1: nothing listens
    assert not log.exists(), f"a GUI tool was invoked: {log.read_text() if log.exists() else ''}"
    assert "CHECKLIST MANUAL EN SAFARI" in r.stdout and "hazla tú" in r.stdout and "no se abre ni se controla ningún navegador" in r.stdout
    assert "El servidor no responde" in r.stdout and r.returncode == 1


def test_checklist_validates_a_real_local_demo_server(tmp_path, monkeypatch):
    import socket
    import uvicorn
    from app.api import create_app
    from app.config import settings
    from app.db import make_engine
    from app.demo_seed import build
    monkeypatch.setattr(settings, "demo_mode", True)
    db = f"sqlite:///{tmp_path / 'demo_comercial_chk.sqlite3'}"
    build(make_engine(db))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(create_app(db), host="127.0.0.1", port=port, log_level="error", proxy_headers=False))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, "qa", "safari_manual_checklist.py"), str(port)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and r.stdout.count("[PASS]") == 4 and "[FAIL]" not in r.stdout, r.stdout
    finally:
        srv.should_exit = True; th.join(timeout=5)
