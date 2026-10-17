#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TUI 端到端测试：在伪终端里驱动 netease_player.py，重建屏幕并快照。

用法：python3 tools/tui_test.py
"""
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "netease_player.py")
SHOT_DIR = os.path.join(ROOT, "screenshots")

CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def char_w(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


class Screen:
    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.buf = [[" "] * cols for _ in range(rows)]
        self.cr = 0
        self.cc = 0

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.buf = [[" "] * cols for _ in range(rows)]
        self.cr = self.cc = 0

    def clear(self) -> None:
        self.buf = [[" "] * self.cols for _ in range(self.rows)]

    def put(self, ch: str) -> None:
        w = char_w(ch)
        if w == 0:
            return
        if self.cc >= self.cols:
            self.cc = 0
            self.cr = min(self.cr + 1, self.rows - 1)
        if 0 <= self.cr < self.rows and 0 <= self.cc < self.cols:
            self.buf[self.cr][self.cc] = ch
            for k in range(1, w):
                if self.cc + k < self.cols:
                    self.buf[self.cr][self.cc + k] = ""
        self.cc += w

    def feed(self, data: bytes) -> None:
        text = data.decode("utf-8", "replace")
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                m = CSI.match(text, i)
                if not m:
                    i += 1
                    continue
                seq = m.group(0)
                body = seq[2:-1]
                final = seq[-1]
                if final == "J" and body in ("2", "3"):
                    self.clear()
                elif final == "K":
                    if 0 <= self.cr < self.rows:
                        for c in range(self.cc, self.cols):
                            self.buf[self.cr][c] = " "
                elif final == "H":
                    parts = (body or "1;1").split(";")
                    try:
                        self.cr = int(parts[0] or 1) - 1
                        self.cc = int(parts[1] or 1) - 1
                    except ValueError:
                        pass
                elif final in ("A", "B", "C", "D"):
                    try:
                        n = int(body or "1") or 1
                    except ValueError:
                        n = 1
                    if final == "A":
                        self.cr = max(0, self.cr - n)
                    elif final == "B":
                        self.cr = min(self.rows - 1, self.cr + n)
                    elif final == "C":
                        self.cc = min(self.cols - 1, self.cc + n)
                    else:
                        self.cc = max(0, self.cc - n)
                i = m.end()
                continue
            if ch == "\r":
                self.cc = 0
            elif ch == "\n":
                self.cr = min(self.cr + 1, self.rows - 1)
            elif ch == "\x08":
                self.cc = max(0, self.cc - 1)
            elif ch == "\x07":
                pass
            else:
                self.put(ch)
            i += 1

    def text(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.buf)


class Driver:
    def __init__(self, cols: int, rows: int, args):
        self.master, slave = pty.openpty()
        self.set_size(cols, rows)
        env = dict(os.environ)
        env.pop("NO_COLOR", None)        # 测试环境可能注入 NO_COLOR
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        env["NCM_CONFIG_DIR"] = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "ncm-test-config")
        self.proc = subprocess.Popen(
            [sys.executable, PLAYER, *args],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=ROOT, env=env, close_fds=True,
        )
        os.close(slave)
        self.screen = Screen(cols, rows)

    def set_size(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, cols: int, rows: int) -> None:
        self.set_size(cols, rows)
        self.screen.resize(cols, rows)
        try:
            os.killpg(os.getpgid(self.proc.pid), 0x12)  # SIGWINCH
        except ProcessLookupError:
            try:
                import signal
                self.proc.send_signal(signal.SIGWINCH)
            except Exception:
                pass

    def read(self, timeout: float = 0.6, idle: float = 0.45) -> None:
        end = time.time() + timeout
        last = time.time()
        while time.time() < end:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.screen.feed(data)
                last = time.time()
            elif time.time() - last > idle:
                break

    def send(self, text: str) -> None:
        os.write(self.master, text.encode("utf-8"))

    def snapshot(self, name: str) -> str:
        os.makedirs(SHOT_DIR, exist_ok=True)
        path = os.path.join(SHOT_DIR, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.screen.text() + "\n")
        return path

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            if self.alive():
                self.proc.terminate()
                self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


def main() -> int:
    failures = []

    def check(cond, label):
        print(("  [PASS] " if cond else "  [FAIL] ") + label)
        if not cond:
            failures.append(label)

    # ---------- 宽屏 ----------
    d = Driver(140, 40, ["--backend", "sim"])
    d.read(2.0)

    def exit_browse(label: str, tries: int = 6) -> bool:
        """退出全屏浏览（先判断再按键，容忍渲染时序抖动；清掉误输入字符）。"""
        for _ in range(tries):
            last = d.screen.text().splitlines()[-1]
            if last.startswith(">"):
                d.send("\x15")                 # ctrl-u 清空可能的误输入
                d.read(0.4)
                return True
            d.send("q")
            d.read(0.9)
        d.snapshot(f"tui-browse-exit-fail-{label}.txt")
        return False
    d.send("search 海阔天空\r")
    d.read(8.0)
    txt = d.screen.text()
    if "海阔天空" not in txt:          # 网络抖动时重试一次
        d.send("search 海阔天空\r")
        d.read(8.0)
        txt = d.screen.text()
    check("搜索「海阔天空」" in txt, "宽屏：搜索结果出现（表头从头显示）")
    check(bool(re.search(r"(?m)^\s*1\. ", txt)), "宽屏：首条结果可见")
    check("浏览" in txt.splitlines()[-1], "宽屏：长输出自动切入全屏浏览")
    check(bool(re.search(r"(?m)^\s*\d+\. .{2,}\S\s{3,}\S+ \d\d:\d\d$", txt)),
          "宽屏：结果含右对齐歌手列")
    check(exit_browse("search"), "宽屏：q 退出浏览回到提示行")

    d.send("play 1\r")
    d.read(7.0)
    txt = d.screen.text()
    check("正在播放" in txt, "宽屏：正在播放面板")
    check("海阔天空" in txt, "宽屏：曲名显示")
    check("歌词" in txt, "宽屏：歌词面板")
    check("模式" in txt and "内核" in txt, "宽屏：状态行（模式/内核）")
    check("░" in txt and "█" in txt, "宽屏：进度条")
    d.snapshot("tui-140x40.txt")

    # 命令历史 / 列表
    d.send("\x1b[A")           # 上方向键 = 历史
    d.read(0.8)
    txt = d.screen.text()
    check("play 1" in txt.splitlines()[-1], "宽屏：上方向键调出历史")
    d.send("\x1b[B\x7f\x7f\x7f\x7f\x7f\x7f")   # 下方向键 + 退格清空
    d.read(0.5)
    d.send("list\r")
    d.read(2.0)
    txt = d.screen.text()
    check("播放队列" in txt, "宽屏：list 显示全部队列（自动浏览）")
    check("浏览" in txt.splitlines()[-1], "宽屏：list 长输出自动进入浏览模式")
    check(bool(re.search(r"(?m)^[▶\s]*1\.\s", txt)), "宽屏：队列从第 1 首开始显示")
    check(bool(re.search(r"▶\s*\d+\.", txt)), "宽屏：当前曲目标记（▶+序号）可见")
    d.send("\x1b[6~")                    # PgDn 翻页
    d.read(0.5)
    d.send("\x1b[5~")                    # PgUp 翻回
    d.read(0.5)
    d.snapshot("tui-queue.txt")
    check(exit_browse("list"), "宽屏：q 退出浏览回到提示行（队列）")

    # Tab 补全
    d.send("sea")
    d.read(0.4)
    d.send("\t")
    d.read(0.6)
    last = d.screen.text().splitlines()[-1]
    check("search" in last, f"宽屏：Tab 补全（当前行 {last!r}）")
    d.send("\x15")              # ctrl-u 清空
    d.read(0.3)

    # ---------- 中屏（80x24） ----------
    d.resize(80, 24)
    d.read(2.0)
    txt = d.screen.text()
    check(len(txt.splitlines()[0]) <= 80, "中屏：首行不超宽")
    check("正在播放" in txt or "海阔天空" in txt, "中屏：播放信息仍在")
    check("歌词" in txt or "00:" in txt, "中屏：面板仍在（歌词/进度）")
    d.snapshot("tui-80x24.txt")

    # ---------- 窄屏（52x16） ----------
    d.resize(52, 16)
    d.read(2.0)
    txt = d.screen.text()
    lines = txt.splitlines()
    check(all(len(ln) <= 52 for ln in lines), "窄屏：每行不超过 52 列")
    check("海阔" in txt or "▶" in txt or "00:" in txt, "窄屏：极简状态行")
    d.snapshot("tui-52x16.txt")

    # ---------- 极小屏（36x10） ----------
    d.resize(36, 10)
    d.read(2.0)
    txt = d.screen.text()
    lines = txt.splitlines()
    check(all(len(ln) <= 36 for ln in lines), "极小屏：每行不超过 36 列")
    d.snapshot("tui-36x10.txt")

    # 回到宽屏继续操作
    d.resize(120, 36)
    d.read(2.0)
    d.send("help\r")
    d.read(2.0)
    txt = d.screen.text()
    check("命令一览" in txt or "例：search 海阔天空" in txt,
          "恢复宽屏：help 正常（长输出自动浏览）")
    exit_browse("help")

    # 暂停 / 继续 / 音量
    d.send("pause\r")
    d.read(1.0)
    txt = d.screen.text()
    check("已暂停" in txt, "暂停命令生效")
    d.send("vol 55\r")
    d.read(1.0)
    txt = d.screen.text()
    check("音量 55%" in txt, "音量命令生效")
    d.send("seek 30\r")
    d.read(1.5)
    txt = d.screen.text()
    check("跳转到" in txt, "seek 命令生效")

    # list clear：一键清空队列
    d.send("list clear\r")
    d.read(2.0)
    check("已清空播放队列" in d.screen.text(), "list clear：清空并提示原数量")
    d.send("list\r")
    d.read(1.5)
    check("播放队列是空的" in d.screen.text(), "list clear：清空后 list 提示为空")

    d.send("quit\r")
    d.read(2.0)
    exited = not d.alive()
    check(exited, "quit 正常退出")
    d.stop()

    print("-" * 46)
    print(f"结果：{len(failures)} 项失败" + ("：" + "；".join(failures) if failures else "（全部通过）"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
