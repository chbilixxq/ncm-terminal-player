#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裸 PTY 尺寸传播实验：TIOCSWINSZ 打在 master / slave 上，子进程看到的尺寸。"""
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time

CHILD = r'''
import os, sys, time, signal
def winch(*a):
    print("SIGWINCH handler fired", flush=True)
def usr1(*a):
    print("SIGUSR1 handler fired", flush=True)
signal.signal(signal.SIGWINCH, winch)
signal.signal(signal.SIGUSR1, usr1)
last = None
end = time.time() + 8
while time.time() < end:
    try:
        ts = os.get_terminal_size(0)
        cur = (ts.columns, ts.lines)
    except OSError as e:
        cur = ("ERR", e)
    if cur != last:
        print("size ->", cur, flush=True)
        last = cur
    time.sleep(0.3)
'''


def size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def drain(fd, secs=1.0):
    out = b""
    end = time.time() + secs
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                d = os.read(fd, 65536)
            except OSError:
                break
            if not d:
                break
            out += d
    return out


def main():
    master, slave = pty.openpty()
    size(master, 100, 30)
    proc = subprocess.Popen([sys.executable, "-u", "-c", CHILD],
                            stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True)
    print("boot(master):", drain(master, 1.0).decode(errors="replace").strip())

    size(master, 80, 24)
    print("after ioctl(master,80x24)+SIGWINCH:",
          drain(master, 1.5).decode(errors="replace").strip())

    size(slave, 60, 20)
    print("after ioctl(slave,60x20)+SIGWINCH:",
          drain(master, 1.5).decode(errors="replace").strip())

    os.kill(proc.pid, 0x12)
    print("after manual SIGWINCH:",
          drain(master, 1.5).decode(errors="replace").strip())
    os.kill(proc.pid, 10)
    print("after manual SIGUSR1:",
          drain(master, 1.5).decode(errors="replace").strip())

    proc.terminate()
    proc.wait(timeout=3)
    os.close(master)
    os.close(slave)


if __name__ == "__main__":
    main()
