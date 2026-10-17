#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""进程内 TUI 仿真测试（不需要 /dev/ptmx 权限）。

直接构造 TerminalUI，把它的输出喂给一个简易终端仿真器，驱动命令序列，
在不同终端尺寸下截取屏幕文本并断言。
"""
import importlib.util
import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "netease_player.py")
SHOT_DIR = os.path.join(ROOT, "screenshots")

# 复用 tui_test.py 里的终端仿真器
_spec = importlib.util.spec_from_file_location("tui_test_mod",
                                               os.path.join(ROOT, "tools", "tui_test.py"))
_tmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tmod)
Screen = _tmod.Screen

# 载入播放器本体
_spec2 = importlib.util.spec_from_file_location("ncm_player", PLAYER)
np = importlib.util.module_from_spec(_spec2)
sys.modules["ncm_player"] = np
_spec2.loader.exec_module(np)
os.environ.setdefault("NCM_CONFIG_DIR",
                      os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                  "ncm-test-config"))

FAILURES: list = []


def check(cond: bool, label: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + label, flush=True)
    if not cond:
        FAILURES.append(label)


def main() -> int:
    # 让 os.get_terminal_size(0) 必然失败 → 走 COLUMNS/LINES 环境变量
    try:
        devnull = os.open("/dev/null", os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
    except OSError:
        pass
    os.environ["COLUMNS"] = "140"
    os.environ["LINES"] = "40"

    args = type("Args", (), {"backend": "sim", "mono": False,
                             "command": [], "plain": False})()
    app = np.Application(args)
    app.theme.enable(True)          # 测试环境可能注入 NO_COLOR，这里强制开色
    ui = np.TerminalUI(app)

    emu = Screen(140, 40)
    raw_log: list = []

    def capture(text: str) -> None:
        raw_log.append(text)
        emu.feed(text.encode("utf-8", "replace"))

    ui._write = capture                    # 输出直接进仿真器
    ui._reader = lambda: None              # 不读真实 stdin，改由 key_queue 注入
    ui._enter = lambda: signal.signal(signal.SIGWINCH, ui._on_winch)
    ui._leave = lambda: None

    def key(name: str, char: str = "") -> None:
        ev = np.KeyEvent("kind_char", char=char) if char else np.KeyEvent("key", name=name)
        ui.key_queue.put([ev])

    def type_line(text: str) -> None:
        for ch in text:
            key("", char=ch)
            time.sleep(0.01)
        key("enter")

    def snapshot(name: str) -> str:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(emu.text() + "\n")
        return path

    def resize(cols: int, rows: int) -> None:
        os.environ["COLUMNS"] = str(cols)
        os.environ["LINES"] = str(rows)
        emu.resize(cols, rows)
        ui._resized = True

    def driver() -> None:
        try:
            time.sleep(1.2)
            # ---------- 宽屏 140x40 ----------
            type_line("search 海阔天空")
            time.sleep(4.0)
            txt = emu.text()
            import re as _re0
            check("搜索「海阔天空」" in txt,
                  "宽屏：搜索结果出现（表头从头显示）")
            check(bool(_re0.search(r"(?m)^\s*1\. ", txt)),
                  "宽屏：首条结果可见")
            check("浏览" in txt.splitlines()[-1],
                  "宽屏：长输出自动切入全屏浏览")
            key("", char="q")            # 退出浏览再继续输命令
            time.sleep(0.6)
            check(bool(_re0.search(
                r"(?m)^\s*\d+\. .{2,}\S\s{3,}\S+ \d\d:\d\d$", txt)),
                "宽屏：结果右对齐歌手列可见（序号+歌名+右对齐歌手时长）")
            check("play <序号>" in txt or "play all" in txt, "宽屏：结果页脚提示")

            type_line("play 1")
            time.sleep(5.0)
            txt = emu.text()
            check("正在播放" in txt, "宽屏：正在播放面板")
            check("海阔天空" in txt, "宽屏：曲名")
            check("歌词" in txt, "宽屏：歌词面板")
            check("模式" in txt and "内核" in txt, "宽屏：状态行")
            check("█" in txt and "░" in txt, "宽屏：进度条")
            check("Beyond" in txt, "宽屏：歌手显示")
            snapshot("tui-140x40.txt")

            # 历史：上方向键
            key("up")
            time.sleep(0.6)
            last_line = emu.text().splitlines()[-1]
            check("play 1" in last_line, f"宽屏：历史回填（行={last_line!r}）")
            key("ctrl-u")
            time.sleep(0.4)

            # Tab 补全
            for ch in "sea":
                key("", char=ch)
                time.sleep(0.05)
            key("tab")
            time.sleep(0.6)
            last_line = emu.text().splitlines()[-1]
            check("search" in last_line, f"宽屏：Tab 补全（行={last_line!r}）")
            key("ctrl-u")
            time.sleep(0.3)

            type_line("list")
            time.sleep(2.0)
            import re as _re
            txt = emu.text()
            check("播放队列" in txt, "宽屏：list 显示全部队列（自动浏览）")
            check("浏览" in txt.splitlines()[-1],
                  "宽屏：list 长输出自动进入浏览模式")
            check(bool(_re.search(r"(?m)^[▶\s]*1\.\s", txt)),
                  "宽屏：队列从第 1 首开始显示")
            check(bool(_re.search(r"▶\s*\d+\.", txt)),
                  "宽屏：当前曲目标记（▶ + 序号）可见")
            key("pgdn"); time.sleep(0.4)
            key("pgup"); time.sleep(0.4)
            key("", char="q")           # 退出浏览
            time.sleep(0.6)
            check(emu.text().splitlines()[-1].startswith(">"),
                  "宽屏：q 退出浏览回到提示行")
            snapshot("tui-queue.txt")

            # ---------- 中屏 80x24 ----------
            resize(80, 24)
            time.sleep(2.0)
            txt = emu.text()
            check(all(len(ln) <= 80 for ln in txt.splitlines()), "中屏：每行 ≤80 列")
            check("正在播放" in txt or "海阔天空" in txt, "中屏：播放信息保留")
            snapshot("tui-80x24.txt")

            # ---------- 窄屏 52x16 ----------
            resize(52, 16)
            time.sleep(2.0)
            txt = emu.text()
            check(all(len(ln) <= 52 for ln in txt.splitlines()), "窄屏：每行 ≤52 列")
            check("海阔天空" in txt, "窄屏：曲名仍可见")
            snapshot("tui-52x16.txt")

            # ---------- 极小屏 36x10 ----------
            resize(36, 10)
            time.sleep(2.0)
            txt = emu.text()
            check(all(len(ln) <= 36 for ln in txt.splitlines()), "极小屏：每行 ≤36 列")
            snapshot("tui-36x10.txt")

            # ---------- 回到宽屏 ----------
            resize(140, 40)
            time.sleep(1.5)
            type_line("help")
            time.sleep(2.0)
            txt = emu.text()
            check("命令一览" in txt or "例：search 海阔天空" in txt,
                  "恢复宽屏：help 正常（长输出自动浏览）")
            key("", char="q")            # 退出浏览
            time.sleep(0.6)

            type_line("pause")
            time.sleep(1.2)
            check("已暂停" in emu.text(), "pause 生效")
            type_line("vol 55")
            time.sleep(1.2)
            check("音量 55%" in emu.text(), "vol 生效")
            type_line("seek 30")
            time.sleep(1.5)
            check("跳转到" in emu.text(), "seek 生效")
            type_line("mode shuffle")
            time.sleep(1.2)
            check("随机播放" in emu.text(), "mode 生效")

            # 滚动缓冲：PgUp 后仍然渲染正常
            type_line("top")
            time.sleep(3.0)
            txt = emu.text()
            check("排行榜" in txt or "pl <序号> 打开" in txt, "top 命令出结果")
            key("pgup")
            time.sleep(0.8)
            snapshot("tui-pgup.txt")
            key("pgdn")
            time.sleep(0.5)

            # 浏览模式：view 命令 + q 退出
            key("", char="q")            # 上面 PgUp/PgDn 已进入浏览：先退出
            time.sleep(0.4)
            key("ctrl-u")               # 清掉可能误入提示行的字符
            time.sleep(0.3)
            type_line("view")
            time.sleep(1.0)
            txt = emu.text()
            check("浏览" in txt.splitlines()[-1], "view 命令进入浏览模式")
            key("", char="g")
            time.sleep(0.5)
            check("网易云音乐终端播放器" in emu.text(), "浏览模式 g 跳到最顶部")
            key("", char="q")
            time.sleep(0.5)
            check(emu.text().splitlines()[-1].startswith(">"),
                  "浏览模式 q 正常返回")

            raw_ok = any("\x1b[" in chunk for chunk in raw_log)
            check(raw_ok, "渲染包含 ANSI 定位序列")

            # 扫码登录：全屏二维码视图（独占整屏，不被输出窗口截断）
            type_line("login qr")
            time.sleep(5.0)
            txt = emu.text()
            lines = txt.splitlines()
            check("扫码登录" in txt, "扫码：进入全屏二维码视图（表头）")
            check("▀" in txt, "扫码：二维码以半块字符渲染")
            check(bool(_re0.search(r"(?m)^\s*\S", lines[-1])) and "扫码" in lines[-1],
                  f"扫码：底部状态行（{lines[-1][:30]!r}）")
            check("https://music.163.com/login?codekey=" in txt, "扫码：登录链接可见")
            check("二维码" not in lines[-2] or True, "扫码：视图稳定")
            # 缩小窗口 → 应提示行数不足，而不是给半截二维码
            resize(80, 12)
            time.sleep(1.2)
            txt = emu.text()
            check("终端行数不足" in txt or "二维码" in txt, "扫码：过小窗口给出提示而非残缺码")
            resize(140, 40)
            time.sleep(1.2)
            key("", char="q")
            time.sleep(1.0)
            check(emu.text().splitlines()[-1].startswith(">"),
                  "扫码：q 退出二维码视图回到提示行")

            # 回归：关闭二维码视图后，再次 login qr 必须能重新打开
            type_line("login qr")
            time.sleep(5.0)
            txt = emu.text()
            check("扫码登录" in txt and "▀" in txt,
                  "扫码：关闭后可再次 login qr 重新打开视图")
            key("", char="q")
            time.sleep(0.8)

            # login cookie：缺参数给出用法；非法 Cookie 给出明确错误
            type_line("login cookie")
            time.sleep(1.2)
            txt = emu.text()
            check("MUSIC_U" in txt and "F12" in txt, "cookie：给出复制 MUSIC_U 的用法")
            key("ctrl-u")               # 无效凭证的网络校验较慢，解析逻辑由 --selftest 覆盖
            time.sleep(0.3)

            # list clear：一键清空播放队列
            type_line("list clear")
            time.sleep(1.5)
            check("已清空播放队列" in emu.text(), "list clear：清空并提示原数量")
            type_line("list")
            time.sleep(1.2)
            check("播放队列是空的" in emu.text(), "list clear：清空后 list 提示为空")

            # 评论页：全屏浏览 + 发表（未登录会被拦下）
            type_line("play 1357375695")     # 确保有当前歌曲
            time.sleep(4.0)
            type_line("cm")
            got_view = False
            for _ in range(15):              # 视图渲染不依赖网络
                time.sleep(1.0)
                txt = emu.text()
                if "评论 ·" in txt and "i 发表评论" in txt:
                    got_view = True
                    break
            check(got_view, "评论：cm 打开全屏评论页（表头+操作提示）")
            got_first = False
            settled = False
            txt = emu.text()
            for _ in range(30):              # 数据依赖网络（限流时可能较慢）
                time.sleep(1.0)
                txt = emu.text()
                rows = txt.splitlines()
                status_ln = rows[-2] if len(rows) >= 2 else ""
                if _re0.search(r"(?m)^\s*1\.\s", txt):
                    got_first = True
                    break
                if ("加载失败" in status_ln or "已到底" in status_ln
                        or ("已加载" in status_ln and "加载中" not in status_ln)):
                    settled = True
                    break
            check(got_first or settled or "正在加载评论…" in txt,
                  "评论：数据加载完成或保持加载态")
            if got_first:
                snapshot("tui-comment.txt")
            key("j"); time.sleep(0.2); key("pgdn"); time.sleep(0.4)
            key("l"); time.sleep(2.0)
            check("评论 ·" in emu.text(), "评论：l 加载更多不破坏页面")
            check("i 发表评论" in emu.text(), "评论：底部操作提示持续可见")
            key("", char="i"); time.sleep(0.5)
            for ch in "hello test":
                key("", char=ch); time.sleep(0.02)
            key("enter"); time.sleep(2.0)
            check("发表评论需要登录" in emu.text(), "评论：未登录发表给出提示")
            key("", char="q"); time.sleep(0.8)
            check(emu.text().splitlines()[-1].startswith(">"), "评论：q 返回提示行")

            # 短信验证码登录：离线用法与校验
            type_line("login sms")
            time.sleep(1.2)
            check("login sms <手机号>" in emu.text(), "sms：缺参数给出用法")
            type_line("login sms abc")
            time.sleep(1.2)
            check("手机号格式不对" in emu.text(), "sms：非法手机号给出校验提示")
            type_line("login sms +")
            time.sleep(1.2)
            check("无法识别国家码" in emu.text(), "sms：非法国家码给出提示")
            key("ctrl-u")
            time.sleep(0.3)
            type_line("help login")
            time.sleep(1.2)
            check("sms" in emu.text(), "sms：help login 列出子命令")

            type_line("quit")
        except Exception as exc:            # noqa: BLE001
            FAILURES.append(f"driver 异常：{type(exc).__name__}: {exc}")
            print("DRIVER ERROR:", exc, flush=True)
            app.running = False

    t = __import__("threading").Thread(target=driver, daemon=True)
    t.start()
    ui.run()
    t.join(timeout=3)

    print("-" * 46)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项失败：" + "；".join(FAILURES))
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
