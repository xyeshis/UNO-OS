#!/usr/bin/env python3
"""Minimal UNO OS CLI terminal (restored)."""

import argparse
import os
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("[!] нужен pyserial: pip install pyserial\n")
    raise


KNOWN_IDS = [
    (0x2341, None), (0x2A03, None), (0x1A86, 0x7523), (0x1A86, 0x55D4),
    (0x10C4, 0xEA60), (0x0403, 0x6001), (0x0403, 0x6015), (0x1B4F, None),
]


def detect_port():
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for v, d in KNOWN_IDS:
            if p.vid == v and (d is None or p.pid == d):
                return p.device
    return None


def list_ports_print():
    ports = list(list_ports.comports())
    if not ports:
        print("  (no ports)")
        return
    for p in ports:
        vid = f"{p.vid:04x}" if p.vid is not None else "----"
        pid = f"{p.pid:04x}" if p.pid is not None else "----"
        print(f"  {p.device:<10} {vid}:{pid} {p.description}")


def run_terminal(ser):
    if os.name == "nt":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if not ch:
                    continue
                if ch == "\x03":
                    break
                ser.write(ch.encode("utf-8", "ignore"))
                ser.flush()
            data = ser.read(256)
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            time.sleep(0.005)
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.01)
                if r:
                    ch = sys.stdin.buffer.read(1)
                    if ch == b"\x03":
                        break
                    ser.write(ch)
                    ser.flush()
                data = ser.read(256)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    ap = argparse.ArgumentParser(description="UNO OS terminal client")
    ap.add_argument("--port", help="COM port, e.g. COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args()

    if args.list_ports:
        list_ports_print()
        return

    port = args.port or detect_port()
    if not port:
        sys.stderr.write("[!] Arduino UNO не найдена. Укажи --port COMx\n")
        list_ports_print()
        return

    print(f"[i] {port} @ {args.baud}")
    try:
        ser = serial.Serial(port, args.baud, timeout=0.01)
    except serial.SerialException as e:
        sys.stderr.write(f"[!] open {port} failed: {e}\n")
        return

    try:
        time.sleep(2.0)  # UNO reset on open
        ser.write(b"\n")
        ser.flush()
        run_terminal(ser)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


# Guard against accidental duplicated content in file:
if os.environ.get("UNOCTL_MAIN_RAN") != "1":
    os.environ["UNOCTL_MAIN_RAN"] = "1"
    if __name__ == "__main__":
        main()
#!/usr/bin/env python3
"""UNO OS Python terminal client."""

import argparse
import os
import queue
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("[!] нужен pyserial: pip install pyserial\n")
    sys.exit(1)

KNOWN_IDS = [
    (0x2341, None), (0x2A03, None), (0x1A86, 0x7523), (0x1A86, 0x55D4),
    (0x10C4, 0xEA60), (0x0403, 0x6001), (0x0403, 0x6015), (0x1B4F, None),
]

CTRL_RBR = 0x1D
PROMPT = b"uno> "
WPROMPT = b"... > "


def detect_port() -> str | None:
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for kv, kp in KNOWN_IDS:
            if p.vid == kv and (kp is None or p.pid == kp):
                return p.device
    return None


def list_all_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("  (ни одного порта не найдено)")
        return
    for p in ports:
        vid = f"{p.vid:04x}" if p.vid is not None else "----"
        pid = f"{p.pid:04x}" if p.pid is not None else "----"
        print(f"  {p.device:<10}  {vid}:{pid}  {p.description}")


class Bridge:
    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.q: queue.Queue[bytes] = queue.Queue()
        self.silent = threading.Event()
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()

    def _reader(self) -> None:
        while not self.stop.is_set():
            try:
                data = self.ser.read(256)
            except Exception:
                break
            if not data:
                continue
            if self.silent.is_set():
                self.q.put(data)
            else:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except Exception:
                    pass

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def begin_silent(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.silent.set()

    def end_silent(self) -> None:
        self.silent.clear()
        while not self.q.empty():
            try:
                d = self.q.get_nowait()
                sys.stdout.buffer.write(d)
            except queue.Empty:
                break
        sys.stdout.buffer.flush()

    def read_until(self, marker: bytes, timeout: float = 5.0) -> bytes:
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            try:
                d = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            buf.extend(d)
            if marker in buf:
                return bytes(buf)
        return bytes(buf)

    def close(self) -> None:
        self.stop.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:
            pass


def upload(br: Bridge, local: str, remote: str) -> None:
    if not os.path.exists(local):
        print(f"[!] нет файла: {local}")
        return
    with open(local, "rb") as f:
        data = f.read()
    lines = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(remote) > 8 or not remote:
        print(f"[!] плохое имя на стороне UNO: '{remote}' (<= 8 символов)")
        return
    projected = sum(len(l) + 1 for l in lines)
    if projected > 116:
        print(f"[!] файл не влезет: {projected} b > 116 b")
        return

    br.begin_silent()
    try:
        br.write(b"\n")
        br.read_until(PROMPT, 1.0)
        br.write(f"write {remote}\n".encode())
        br.read_until(WPROMPT, 2.0)
        for ln in lines:
            br.write(ln + b"\n")
            br.read_until(WPROMPT, 2.0)
        br.write(b".\n")
        br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()
    print(f"[i] upload: {len(data)} b -> {remote}")


def download(br: Bridge, remote: str, local: str) -> None:
    br.begin_silent()
    try:
        br.write(b"\n")
        br.read_until(PROMPT, 1.0)
        br.write(f"cat {remote}\n".encode())
        out = br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()

    txt = out.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in txt:
        head, body = txt.split("\n", 1)
        if head.strip().startswith("cat "):
            txt = body
    if txt.endswith("uno> "):
        txt = txt[:-5]
    txt = txt.rstrip("\n")
    if "? not found" in txt:
        print(f"[!] на UNO нет файла '{remote}'")
        return
    with open(local, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt + "\n")
    print(f"[i] download: {remote} -> {local}")


def menu(br: Bridge) -> bool:
    sys.stdout.buffer.write(b"\r\n[ uno menu | u local [remote] | d remote [local] | l | x ]\r\n> ")
    sys.stdout.buffer.flush()
    if os.name == "nt":
        import msvcrt
        buf = bytearray()
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                break
            if ch == "\x03":
                print("^C")
                return False
            if ch == "\x08":
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            sys.stdout.write(ch)
            sys.stdout.flush()
            buf.extend(ch.encode("utf-8", "ignore"))
        cmd = bytes(buf).decode("utf-8", "ignore").strip()
    else:
        cmd = input().strip()

    parts = cmd.split()
    if not parts:
        print("[ -> terminal ]")
        return False
    op = parts[0]
    if op in ("q", "x", "quit", "exit"):
        return True
    if op in ("u", "upload") and len(parts) >= 2:
        upload(br, parts[1], parts[2] if len(parts) > 2 else os.path.basename(parts[1]))
    elif op in ("d", "download", "down") and len(parts) >= 2:
        download(br, parts[1], parts[2] if len(parts) > 2 else parts[1])
    elif op in ("l", "ls"):
        for f in sorted(os.listdir(".")):
            print(" ", f)
    else:
        print("usage: u local [remote] | d remote [local] | l | x")
    return False


def run_terminal(br: Bridge) -> None:
    if os.name == "nt":
        import msvcrt
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue
            ch = msvcrt.getwch()
            if not ch:
                continue
            if ord(ch) == CTRL_RBR:
                if menu(br):
                    return
                continue
            br.write(ch.encode("utf-8", "ignore"))
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    continue
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    continue
                if ch == bytes([CTRL_RBR]):
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    try:
                        if menu(br):
                            return
                    finally:
                        tty.setraw(fd)
                    continue
                br.write(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> None:
    ap = argparse.ArgumentParser(description="UNO OS Python terminal client")
    ap.add_argument("--port", help="force serial port (COM7, /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args()

    if args.list_ports:
        list_all_ports()
        return

    port = args.port or detect_port()
    if not port:
        sys.stderr.write("[!] Arduino UNO не найдена. Укажи --port COMx\n")
        list_all_ports()
        sys.exit(2)

    print(f"[i] {port} @ {args.baud}")
    try:
        br = Bridge(port, args.baud)
    except serial.SerialException as e:
        msg = str(e)
        sys.stderr.write(f"[!] не удалось открыть {port}: {msg}\n")
        if "PermissionError" in msg or "Access is denied" in msg or "Отказано" in msg:
            sys.stderr.write("[?] порт занят другой программой (Serial Monitor / PuTTY / другой клиент)\n")
        sys.exit(3)

    time.sleep(2.0)
    try:
        if args.upload:
            upload(br, args.upload[0], args.upload[1])
        elif args.download:
            download(br, args.download[0], args.download[1])
        else:
            print("[i] терминал. Ctrl+] -> меню upload/download/quit")
            run_terminal(br)
    except KeyboardInterrupt:
        print("\n[i] прервано Ctrl+C")
    finally:
        br.close()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
unoctl.py - Python terminal client for UNO OS.
"""

import argparse
import os
import queue
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("[!] нужен pyserial: pip install pyserial\n")
    sys.exit(1)


KNOWN_IDS = [
    (0x2341, None), (0x2A03, None), (0x1A86, 0x7523), (0x1A86, 0x55D4),
    (0x10C4, 0xEA60), (0x0403, 0x6001), (0x0403, 0x6015), (0x1B4F, None),
]

CTRL_RBR = 0x1D  # Ctrl+]
PROMPT = b"uno> "
WPROMPT = b"... > "


def detect_port() -> str | None:
    for p in list_ports.comports():
        if p.vid is None:
            continue
        for kv, kp in KNOWN_IDS:
            if p.vid == kv and (kp is None or p.pid == kp):
                return p.device
    return None


def list_all_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("  (ни одного порта не найдено)")
        return
    for p in ports:
        vid = f"{p.vid:04x}" if p.vid is not None else "----"
        pid = f"{p.pid:04x}" if p.pid is not None else "----"
        print(f"  {p.device:<10}  {vid}:{pid}  {p.description}")


class Bridge:
    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.q: queue.Queue[bytes] = queue.Queue()
        self.silent = threading.Event()
        self.stop = threading.Event()
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()

    def _reader(self) -> None:
        while not self.stop.is_set():
            try:
                data = self.ser.read(256)
            except Exception:
                break
            if not data:
                continue
            if self.silent.is_set():
                self.q.put(data)
            else:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except Exception:
                    pass

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def begin_silent(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.silent.set()

    def end_silent(self) -> None:
        self.silent.clear()
        while not self.q.empty():
            try:
                d = self.q.get_nowait()
                sys.stdout.buffer.write(d)
            except queue.Empty:
                break
        sys.stdout.buffer.flush()

    def read_until(self, marker: bytes, timeout: float = 5.0) -> bytes:
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            try:
                d = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            buf.extend(d)
            if marker in buf:
                return bytes(buf)
        return bytes(buf)

    def close(self) -> None:
        self.stop.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:
            pass


def upload(br: Bridge, local: str, remote: str) -> None:
    if not os.path.exists(local):
        print(f"[!] нет файла: {local}")
        return
    with open(local, "rb") as f:
        data = f.read()
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = text.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    if len(remote) > 8 or not remote:
        print(f"[!] плохое имя на стороне UNO: '{remote}' (<= 8 символов)")
        return
    projected = sum(len(l) + 1 for l in lines)
    if projected > 116:
        print(f"[!] файл не влезет: {projected} b > 116 b")
        return

    br.begin_silent()
    try:
        br.write(b"\n")
        br.read_until(PROMPT, 1.0)
        br.write(f"write {remote}\n".encode())
        br.read_until(WPROMPT, 2.0)
        for ln in lines:
            br.write(ln + b"\n")
            br.read_until(WPROMPT, 2.0)
        br.write(b".\n")
        br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()
    print(f"[i] upload: {len(data)} b -> {remote}")


def download(br: Bridge, remote: str, local: str) -> None:
    br.begin_silent()
    try:
        br.write(b"\n")
        br.read_until(PROMPT, 1.0)
        br.write(f"cat {remote}\n".encode())
        out = br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()

    txt = out.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in txt:
        head, body = txt.split("\n", 1)
        if head.strip().startswith("cat "):
            txt = body
    if txt.endswith("uno> "):
        txt = txt[:-len("uno> ")]
    txt = txt.rstrip("\n")
    if "? not found" in txt:
        print(f"[!] на UNO нет файла '{remote}'")
        return
    with open(local, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt + "\n")
    print(f"[i] download: {remote} -> {local}")


def menu(br: Bridge) -> bool:
    sys.stdout.buffer.write(
        b"\r\n[ uno menu | u local [remote] | d remote [local] | l | x ]\r\n> "
    )
    sys.stdout.buffer.flush()

    if os.name == "nt":
        import msvcrt
        line_bytes = bytearray()
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                print()
                break
            if ch == "\x03":
                print("^C")
                return False
            if ch == "\x08":
                if line_bytes:
                    line_bytes.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            sys.stdout.write(ch)
            sys.stdout.flush()
            line_bytes.extend(ch.encode("utf-8", "ignore"))
        cmd = bytes(line_bytes).decode("utf-8", "ignore").strip()
    else:
        cmd = input().strip()

    parts = cmd.split()
    if not parts:
        print("[ -> terminal ]")
        return False
    op = parts[0]
    if op in ("q", "x", "quit", "exit"):
        return True
    if op in ("u", "upload") and len(parts) >= 2:
        local = parts[1]
        remote = parts[2] if len(parts) >= 3 else os.path.basename(local)
        upload(br, local, remote)
    elif op in ("d", "download", "down") and len(parts) >= 2:
        remote = parts[1]
        local = parts[2] if len(parts) >= 3 else remote
        download(br, remote, local)
    elif op in ("l", "ls"):
        for f in sorted(os.listdir(".")):
            print(" ", f)
    else:
        print("usage: u local [remote] | d remote [local] | l | x")
    return False


def run_terminal(br: Bridge) -> None:
    if os.name == "nt":
        import msvcrt
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.01)
                continue
            ch = msvcrt.getwch()
            if not ch:
                continue
            if ord(ch) == CTRL_RBR:
                if menu(br):
                    return
                continue
            br.write(ch.encode("utf-8", "ignore"))
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    continue
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    continue
                if ch == bytes([CTRL_RBR]):
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    try:
                        if menu(br):
                            return
                    finally:
                        tty.setraw(fd)
                    continue
                br.write(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main() -> None:
    ap = argparse.ArgumentParser(description="UNO OS Python terminal client")
    ap.add_argument("--port", help="force serial port (COM7, /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"))
    ap.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--list-ports", action="store_true")
    args = ap.parse_args()

    if args.list_ports:
        list_all_ports()
        return

    port = args.port or detect_port()
    if not port:
        sys.stderr.write("[!] Arduino UNO не найдена. Укажи --port COMx\n")
        list_all_ports()
        sys.exit(2)

    print(f"[i] {port} @ {args.baud}")
    try:
        br = Bridge(port, args.baud)
    except serial.SerialException as e:
        msg = str(e)
        sys.stderr.write(f"[!] не удалось открыть {port}: {msg}\n")
        if "PermissionError" in msg or "Access is denied" in msg or "Отказано" in msg:
            sys.stderr.write("[?] порт занят другой программой (Serial Monitor / PuTTY / другой клиент)\n")
        sys.exit(3)

    time.sleep(2.0)  # UNO reset on open
    try:
        if args.upload:
            upload(br, args.upload[0], args.upload[1])
        elif args.download:
            download(br, args.download[0], args.download[1])
        else:
            print("[i] терминал. Ctrl+] -> меню upload/download/quit")
            run_terminal(br)
    except KeyboardInterrupt:
        print("\n[i] прервано Ctrl+C")
    finally:
        br.close()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
unoctl.py — Python-клиент-терминал для UNO OS.

Делает три вещи:
  1. сам ищет подключённую Arduino UNO по VID/PID и открывает Serial @115200;
  2. работает как прозрачный терминал — всё, что приходит с UNO, печатается
     в этот же Python-процесс; всё, что ты набираешь, отдаётся на UNO;
  3. по `Ctrl+]` открывается встроенное меню для upload/download файлов
     в EEPROM-FS платы — без отключения от шелла.

Можно и из командной строки одной командой:

    python unoctl.py --upload  local.py  boot
    python unoctl.py --download boot     dump.py
    python unoctl.py --port COM5         # форсировать порт

Зависимость: pyserial.

    pip install pyserial
"""

import argparse
import os
import queue
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("[!] нужен pyserial:  pip install pyserial\n")
    sys.exit(1)


# --- VID/PID известных Arduino UNO + клонов ----------------------------------

KNOWN_IDS = [
    (0x2341, None),     # Arduino LLC (включая UNO R3 official)
    (0x2A03, None),     # Arduino SRL
    (0x1A86, 0x7523),   # CH340 (большинство дешёвых клонов)
    (0x1A86, 0x55D4),   # CH9102
    (0x10C4, 0xEA60),   # CP210x
    (0x0403, 0x6001),   # FT232R (старые UNO/Nano)
    (0x0403, 0x6015),   # FT231X
    (0x16C0, 0x0483),   # Teensyduino-like
    (0x1B4F, None),     # SparkFun
]

CTRL_RBR = 0x1D         # Ctrl+]  — выход в меню
PROMPT   = b"uno> "
WPROMPT  = b"... > "    # внутренний prompt write-режима


def detect_port() -> str | None:
    for p in list_ports.comports():
        vid, pid = p.vid, p.pid
        if vid is None:
            continue
        for kv, kp in KNOWN_IDS:
            if vid == kv and (kp is None or pid == kp):
                return p.device
    return None


def list_all_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("  (ни одного порта не найдено)")
        return
    for p in ports:
        vid = f"{p.vid:04x}" if p.vid is not None else "----"
        pid = f"{p.pid:04x}" if p.pid is not None else "----"
        print(f"  {p.device:<10}  {vid}:{pid}  {p.description}")


# --- Bridge: фон-ридер + транзакции ------------------------------------------

class Bridge:
    """
    Один фон-поток постоянно вычитывает Serial.
    В обычном режиме байты идут в stdout (живой терминал).
    В silent-режиме они кладутся в очередь — это нужно для upload/download.
    """

    def __init__(self, port: str, baud: int = 115200):
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.q: queue.Queue[bytes] = queue.Queue()
        self.silent = threading.Event()
        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._reader, daemon=True)
        self._thr.start()

    def _reader(self) -> None:
        while not self._stop.is_set():
            try:
                data = self.ser.read(256)
            except Exception:
                break
            if not data:
                continue
            if self.silent.is_set():
                self.q.put(data)
            else:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except Exception:
                    pass

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def begin_silent(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.silent.set()

    def end_silent(self) -> None:
        self.silent.clear()
        # сольём остатки в stdout, чтобы пользователь увидел всё, что прилетело
        while not self.q.empty():
            try:
                d = self.q.get_nowait()
                sys.stdout.buffer.write(d)
            except queue.Empty:
                break
        sys.stdout.buffer.flush()

    def read_until(self, marker: bytes, timeout: float = 5.0) -> bytes:
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            try:
                d = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            buf.extend(d)
            if marker in buf:
                return bytes(buf)
        return bytes(buf)

    def close(self) -> None:
        self._stop.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:
            pass


# --- Upload / Download через shell-команды UNO -------------------------------

def upload(br: Bridge, local: str, remote: str) -> None:
    if not os.path.exists(local):
        print(f"[!] нет файла: {local}")
        return
    with open(local, "rb") as f:
        data = f.read()
    text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = text.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()                              # съедаем пустой хвост от завершающего \n

    # на UNO имена ≤ 8 символов
    if len(remote) > 8 or not remote:
        print(f"[!] плохое имя на стороне UNO: '{remote}' (≤ 8 символов)")
        return
    # 116 байт — лимит одного слота, +1 \n на строку
    proj = sum(len(l) + 1 for l in lines)
    if proj > 116:
        print(f"[!] файл не влезет: {proj} b > 116 b. Сократи.")
        return

    br.begin_silent()
    try:
        br.write(b"\n")                          # сбросим возможный полу-набор
        br.read_until(PROMPT, 1.0)
        br.write(f"write {remote}\n".encode())
        br.read_until(WPROMPT, 2.0)
        for ln in lines:
            br.write(ln + b"\n")
            br.read_until(WPROMPT, 2.0)
        br.write(b".\n")
        br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()
    print(f"[i] upload: {len(data)} b  ->  {remote}")


def download(br: Bridge, remote: str, local: str) -> None:
    br.begin_silent()
    try:
        br.write(b"\n")
        br.read_until(PROMPT, 1.0)
        br.write(f"cat {remote}\n".encode())
        out = br.read_until(PROMPT, 3.0)
    finally:
        br.end_silent()

    txt = out.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")
    # отрежем эхо команды (первая строка), отрежем хвостовой prompt
    if "\n" in txt:
        head, body = txt.split("\n", 1)
        if head.strip().startswith("cat "):
            txt = body
    if txt.endswith("uno> "):
        txt = txt[:-len("uno> ")]
    txt = txt.rstrip("\n")
    if "? not found" in txt:
        print(f"[!] на UNO нет файла '{remote}'")
        return
    with open(local, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt + "\n")
    print(f"[i] download: {remote}  ->  {local}, {len(txt) + 1} b")


# --- Меню Ctrl+] -------------------------------------------------------------

def menu(br: Bridge) -> bool:
    """Возвращает True, если пользователь хочет выйти из всей программы."""
    sys.stdout.buffer.write(
        b"\r\n[ uno menu | u local [remote] | d remote [local] | l | x ]\r\n> "
    )
    sys.stdout.buffer.flush()

    if os.name == "nt":
        line_bytes = bytearray()
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break
            if ch == "\x03":            # Ctrl+C — выйти из меню
                sys.stdout.write("^C\r\n")
                return False
            if ch == "\x08":            # backspace
                if line_bytes:
                    line_bytes.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            sys.stdout.write(ch)
            sys.stdout.flush()
            line_bytes.extend(ch.encode("utf-8", "ignore"))
        cmd = bytes(line_bytes).decode("utf-8", "ignore").strip()
    else:
        # на Unix главный поток в raw-mode — временно вернёмся в canonical mode
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] |= termios.ICANON | termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, new)
        try:
            cmd = sys.stdin.readline().strip()
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old)

    parts = cmd.split()
    if not parts:
        print("[ -> terminal ]")
        return False
    op = parts[0]
    if op in ("q", "x", "quit", "exit"):
        return True
    if op in ("u", "upload") and len(parts) >= 2:
        local = parts[1]
        remote = parts[2] if len(parts) >= 3 else os.path.basename(local)
        upload(br, local, remote)
    elif op in ("d", "download", "down") and len(parts) >= 2:
        remote = parts[1]
        local = parts[2] if len(parts) >= 3 else remote
        download(br, remote, local)
    elif op in ("l", "ls"):
        for f in sorted(os.listdir(".")):
            print(" ", f)
    elif op in ("?", "h", "help"):
        print("u <local> [remote]   — залить локальный файл в FS UNO")
        print("d <remote> [local]   — скачать файл из FS UNO")
        print("l                    — показать локальные файлы")
        print("x                    — выйти из unoctl")
        print("(пустая строка       — обратно в терминал)")
    else:
        print("usage: u local [remote] | d remote [local] | l | x")
    return False


# --- Интерактивный терминал --------------------------------------------------

def run_terminal(br: Bridge) -> None:
    if os.name == "nt":
        import msvcrt
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if not ch:
                    continue
                if ord(ch) == CTRL_RBR:
                    if menu(br):
                        return
                    continue
                br.write(ch.encode("utf-8", "ignore"))
            else:
                time.sleep(0.01)
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not r:
                    continue
                ch = sys.stdin.buffer.read(1)
                if not ch:
                    continue
                if ch == bytes([CTRL_RBR]):
                    # выйдем из raw на время меню
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    try:
                        if menu(br):
                            return
                    finally:
                        tty.setraw(fd)
                    continue
                br.write(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- main --------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="unoctl",
        description="Python client / terminal для UNO OS.",
    )
    ap.add_argument("--port",     help="принудительно указать порт (COM5, /dev/ttyUSB0)")
    ap.add_argument("--baud",     type=int, default=115200)
    ap.add_argument("--upload",   nargs=2, metavar=("LOCAL", "REMOTE"),
                    help="залить локальный файл в FS UNO и выйти")
    ap.add_argument("--download", nargs=2, metavar=("REMOTE", "LOCAL"),
                    help="скачать файл из FS UNO и выйти")
    ap.add_argument("--list-ports", action="store_true",
                    help="показать все доступные COM-порты и выйти")
    args = ap.parse_args()

    if args.list_ports:
        list_all_ports()
        return

    port = args.port or detect_port()
    if not port:
        sys.stderr.write("[!] Arduino UNO не найдена. Проверь USB или укажи --port COMx.\n")
        sys.stderr.write("[?] вижу такие порты:\n")
        list_all_ports()
        sys.exit(2)

    print(f"[i] {port} @ {args.baud}")
    try:
        br = Bridge(port, args.baud)
    except serial.SerialException as e:
        msg = str(e)
        sys.stderr.write(f"[!] не удалось открыть {port}: {msg}\n")
        if "PermissionError" in msg or "Access is denied" in msg or "Отказано" in msg:
            sys.stderr.write("[?] порт занят другой программой. Проверь:\n")
            sys.stderr.write("    - Arduino IDE Serial Monitor открыт? Закрой его (Ctrl+Shift+M).\n")
            sys.stderr.write("    - PuTTY / Windows Terminal / другой unoctl уже подключён?\n")
            sys.stderr.write("    - В Arduino IDE прямо сейчас идёт Upload? Подожди.\n")
            sys.stderr.write("    - На Linux — добавь себя в группу dialout: sudo usermod -aG dialout $USER\n")
        elif "FileNotFoundError" in msg or "could not open port" in msg:
            sys.stderr.write("[?] порта больше нет (плату отключили?). Проверь USB.\n")
        sys.exit(3)
    time.sleep(2.0)                              # UNO сбрасывается при открытии порта

    try:
        if args.upload:
            upload(br, args.upload[0], args.upload[1])
        elif args.download:
            download(br, args.download[0], args.download[1])
        else:
            print("[i] терминал. Ctrl+] — меню (upload/download/quit).")
            run_terminal(br)
    except KeyboardInterrupt:
        print("\n[i] прервано Ctrl+C")
    finally:
        br.close()


if __name__ == "__main__":
    main()
