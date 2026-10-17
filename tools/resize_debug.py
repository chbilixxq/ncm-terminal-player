#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚焦调试：真实 PTY 下 resize 时子进程到底发出了什么。"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "netease_player.py")
OUT = os.path.join(ROOT, "screenshots", "resize_raw.txt")


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def read_all(fd, seconds):
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            buf += data
    return buf


def main():
    master, slave = pty.openpty()
    set_size(master, 140, 40)
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="140",
               LINES="40", NCM_DEBUG="1",
               NCM_CONFIG_DIR="/tmp/ncm-test-config")
    proc = subprocess.Popen([sys.executable, PLAYER, "--backend", "sim"],
                            stdin=slave, stdout=slave, stderr=slave,
                            cwd=ROOT, env=env, close_fds=True)
    os.close(slave)
    time.sleep(1.5)
    read_all(master, 1.5)
    os.write(master, b"search \xe6\xb5\xb7\xe9\x98\x94\xe5\xa4\xa9\xe7\xa9\xba\r")
    time.sleep(4)
    first = read_all(master, 2.0)
    print("阶段1 输出字节:", len(first))

    set_size(master, 80, 24)
    os.kill(proc.pid, 0x12)              # 只给子进程发 SIGWINCH
    time.sleep(1.0)
    second = read_all(master, 3.0)
    print("resize 后输出字节:", len(second))
    print("前 400 字节 repr:")
    print(repr(second[:400]))

    os.write(master, b"quit\r")
    time.sleep(1.0)
    tail = read_all(master, 1.0)
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except Exception:
        proc.kill()
    os.close(master)

    with open(OUT, "wb") as fh:
        fh.write(second + b"\n---TAIL---\n" + tail)
    print("saved:", OUT, "退出码:", proc.returncode)


if __name__ == "__main__":
    main()
