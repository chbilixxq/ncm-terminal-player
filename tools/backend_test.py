#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实播放后端（ffplay / ffmpeg 兜底）端到端测试。

验证：播放推进、暂停保持、恢复、跳转、播放到末尾自动切下一首。

用法：
  python3 tools/backend_test.py            # ffmpeg null 输出（无声）
  ALSA_CONFIG_PATH=... python3 tools/backend_test.py   # 走 ALSA 输出
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importlib.util

spec = importlib.util.spec_from_file_location("ncm_player",
                                              os.path.join(ROOT, "netease_player.py"))
np = importlib.util.module_from_spec(spec)
sys.modules["ncm_player"] = np
spec.loader.exec_module(np)
os.environ.setdefault("NCM_CONFIG_DIR",
                      os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                  "ncm-test-config"))

FAILURES = []


def check(cond, label, detail=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + label
          + (f"  ({detail})" if detail else ""), flush=True)
    if not cond:
        FAILURES.append(label)


def pump_until(app, seconds, step=0.2):
    end = time.time() + seconds
    while time.time() < end:
        app.pump_events()
        time.sleep(step)


def main() -> int:
    backend_name = sys.argv[1] if len(sys.argv) > 1 else "ffplay"
    args = type("Args", (), {"backend": backend_name, "mono": False,
                             "command": [], "plain": False})()
    app = np.Application(args)
    app.theme.enable(False)
    backend = type(app.engine.backend)
    print(f"期望后端：{backend_name}  实际类：{backend.name}  "
          f"实际工具：{app.engine.backend_name}")
    if backend.name == "sim" and backend_name != "sim":
        print(f"  [SKIP] 系统里没有可用的 {backend_name}，跳过测试")
        return 0

    app.execute("search 海阔天空")
    app.execute("play 1")
    pump_until(app, 4.0)
    eng = app.engine
    check(eng.state == "playing", "开始播放", f"state={eng.state}")
    check(eng.current is not None, "当前曲目已加载",
          eng.current.name if eng.current else "-")

    # 1) 进度推进
    p1 = eng.position()
    pump_until(app, 4.0)
    p2 = eng.position()
    check(p2 - p1 >= 2.5, "进度随播放推进", f"{p1:.2f}s → {p2:.2f}s")

    # 2) 暂停
    msg = eng.toggle_pause()
    pump_until(app, 3.0)
    p3 = eng.position()
    check(eng.state == "paused", "暂停状态", msg)
    check(abs(p3 - p2) <= 1.2, "暂停期间进度保持", f"{p2:.2f} → {p3:.2f}")

    # 3) 恢复
    msg = eng.toggle_pause()
    pump_until(app, 3.0)
    p4 = eng.position()
    check(eng.state == "playing", "恢复播放", msg)
    check(p4 - p3 >= 1.5, "恢复后进度继续", f"{p3:.2f} → {p4:.2f}")

    # 4) 跳转（按当前曲目真实可听长度取中点：VIP 歌曲可能只有 30s 试听）
    target = max(5.0, (eng.duration or 60.0) * 0.4)
    eng.seek_to(target)
    pump_until(app, 2.5)
    p5 = eng.position()
    check(abs(p5 - target) <= 4.0, f"seek 到 {target:.1f}s", f"pos={p5:.2f}")

    # 5) 播到末尾自动切歌
    first_id = eng.current.id if eng.current else 0
    duration = eng.duration or (eng.current.duration_s if eng.current else 0)
    eng.seek_to(max(0.0, duration - 3.0))
    pump_until(app, 9.0)
    second_id = eng.current.id if eng.current else 0
    check(second_id != first_id, "到尾自动切换下一首",
          f"{first_id} → {second_id}")
    check(eng.state in ("playing", "loading", "paused"), "切歌后状态正常",
          eng.state)

    eng.stop()
    pump_until(app, 1.0)
    check(eng.state == "idle", "stop 停止", eng.state)
    app.shutdown()

    print("-" * 46)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败：" + "；".join(FAILURES))
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
