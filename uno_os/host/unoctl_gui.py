#!/usr/bin/env python3
"""
unoctl_gui.py — Windows XP desktop-style GUI для UNO OS.

Стилизовано максимально близко к референсу:
- desktop wallpaper + иконки слева;
- taskbar снизу с зелёной start-кнопкой и часами;
- двухколоночное start-меню;
- окно "UNO OS Terminal" в стиле XP (синий title bar + cmd-like terminal).
"""

from __future__ import annotations

import contextlib
import io
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.stderr.write("[!] нужен pyserial:  pip install pyserial\n")
    sys.exit(1)

# подцепим логику из соседнего unoctl.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unoctl import detect_port, download, upload  # noqa: E402


# === XP Luna палитра + шрифты ===============================================

XP = {
    "desktop_blue": "#5A8BD6",
    "task_top": "#2A63E6",
    "task_mid": "#3C79EA",
    "task_bot": "#2255CE",
    "start_top": "#6AC05B",
    "start_mid": "#4FA544",
    "start_bot": "#3E8E3A",
    "start_hover": "#7ED36E",
    "title_top": "#0058E6",
    "title_mid": "#1A6EF0",
    "title_bot": "#0058E6",
    "title_fg": "#FFFFFF",
    "window_bg": "#ECE9D8",
    "beige": "#ECE9D8",
    "window_border": "#0A246A",
    "window_shadow": "#7A7A7A",
    "text": "#000000",
    "term_bg": "#000000",
    "term_fg": "#C0C0C0",
    "term_info": "#9ED2FF",
    "term_err": "#FF8D8D",
}

FONT_UI = ("Tahoma", 8)
FONT_UI_B = ("Tahoma", 8, "bold")
FONT_TITLE = ("Tahoma", 8, "bold")
FONT_MONO = ("Lucida Console", 10)
FONT_START = ("Tahoma", 11, "bold italic")


def paint_vgradient(canvas: tk.Canvas, c_top: str, c_mid: str, c_bot: str,
                    tag: str = "grad") -> None:
    """Залить канвас вертикальным трёх-стоповым градиентом."""
    canvas.delete(tag)
    w = max(1, canvas.winfo_width())
    h = max(1, canvas.winfo_height())

    def hex2rgb(s: str) -> tuple[int, int, int]:
        return int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)

    rt, gt, bt = hex2rgb(c_top)
    rm, gm, bm = hex2rgb(c_mid)
    rb, gb, bb = hex2rgb(c_bot)
    half = max(1, h // 2)
    for i in range(h):
        if i < half:
            t = i / half
            r = rt + (rm - rt) * t
            g = gt + (gm - gt) * t
            b = bt + (bm - bt) * t
        else:
            t = (i - half) / max(1, h - half)
            r = rm + (rb - rm) * t
            g = gm + (gb - gm) * t
            b = bm + (bb - bm) * t
        canvas.create_line(0, i, w, i,
                           fill=f"#{int(r):02x}{int(g):02x}{int(b):02x}",
                           tags=tag)
    canvas.tag_lower(tag)


# === Bridge для GUI ==========================================================

class GuiBridge:
    def __init__(self, port: str, baud: int, on_data) -> None:
        self.ser = serial.Serial(port, baud, timeout=0.05)
        self.on_data = on_data
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
                    self.on_data(data)
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
                self.on_data(d)
            except queue.Empty:
                break

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


# === Главное приложение =====================================================

class XpApp:
    W = 1200
    H = 760

    def __init__(self) -> None:
        self.bridge: GuiBridge | None = None
        self.history: list[str] = []
        self.history_idx = 0
        self.start_menu: tk.Toplevel | None = None
        self.wallpaper_img: tk.PhotoImage | None = None

        self.root = tk.Tk()
        self.root.title("UNO OS — Windows XP style")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - self.W) // 2)
        y = max(0, (sh - self.H) // 2)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.root.minsize(980, 620)
        self.root.configure(bg=XP["desktop_blue"])

        self._setup_styles()
        self._build_desktop()
        self._build_terminal_window()
        self._build_taskbar()
        self._refresh_ports()
        self._tick_clock()
        self.root.bind("<Button-1>", self._click_anywhere, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    # ---- ttk style для XP-кнопок -------------------------------------------

    def _setup_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("XP.TButton", background="#ECE9D8", foreground="#000000",
                        relief="raised", padding=(8, 3), font=FONT_UI)
        style.map(
            "XP.TButton",
            background=[("active", "#FFE893"), ("pressed", "#D5D0BC")],
            relief=[("pressed", "sunken")],
        )
        style.configure("XP.TLabel", background=XP["window_bg"], font=FONT_UI)

    # ---- desktop -----------------------------------------------------------

    def _build_desktop(self) -> None:
        self.desktop = tk.Canvas(self.root, bg=XP["desktop_blue"], highlightthickness=0)
        self.desktop.pack(fill="both", expand=True)
        self.desktop.bind("<Configure>", self._paint_wallpaper)

        # desktop icons (левый столбик)
        self.icons_layer = tk.Frame(self.desktop, bg="", padx=8, pady=12)
        self.desktop.create_window(8, 8, anchor="nw", window=self.icons_layer)
        self._desktop_icon("My UNO", self._focus_terminal)
        self._desktop_icon("Upload", self._upload_dlg)
        self._desktop_icon("Download", self._download_dlg)
        self._desktop_icon("Run script", self._run_dlg)
        self._desktop_icon("File Manager", self.open_file_manager)
        self._desktop_icon("Script Editor", self.open_script_editor)
        self._desktop_icon("Device Panel", self.open_device_panel)
        self._desktop_icon("Connect", self._connect)

    def _desktop_icon(self, text: str, command) -> None:
        wrap = tk.Frame(self.icons_layer, bg=XP["desktop_blue"], pady=2)
        wrap.pack(anchor="w")
        btn = tk.Button(wrap, text="■", width=2, height=1, command=command,
                        bg="#d9ecff", fg="#0e3977", relief="ridge", bd=1)
        btn.pack(side="left")
        lbl = tk.Label(wrap, text=text, fg="white", bg=XP["desktop_blue"], font=FONT_UI)
        lbl.pack(side="left", padx=6)
        lbl.bind("<Double-Button-1>", lambda _e: command())

    def _paint_wallpaper(self, _e=None) -> None:
        self.desktop.delete("wallpaper")
        w = self.desktop.winfo_width()
        h = self.desktop.winfo_height()
        path_candidates = [
            os.path.join(os.path.dirname(__file__), "xp_wallpaper.png"),
            r"C:\Users\focum\.cursor\projects\c-Users-focum-OneDrive-1-arduino-uno\assets\c__Users_focum_AppData_Roaming_Cursor_User_workspaceStorage_e0cc4956d495eadc6776ba91d6bd04d9_images_image-dcb1747f-78ab-46ac-94dc-f4b6cbccb095.png",
        ]
        wp = None
        for p in path_candidates:
            if os.path.exists(p):
                try:
                    wp = tk.PhotoImage(file=p)
                    break
                except tk.TclError:
                    continue
        if wp is not None:
            self.wallpaper_img = wp
            self.desktop.create_image(0, 0, anchor="nw", image=self.wallpaper_img, tags="wallpaper")
        else:
            # fallback sky/grass
            self.desktop.create_rectangle(0, 0, w, int(h * 0.62), fill="#58a9ff", outline="", tags="wallpaper")
            self.desktop.create_rectangle(0, int(h * 0.62), w, h, fill="#57b22f", outline="", tags="wallpaper")
        self.desktop.tag_lower("wallpaper")

    # ---- terminal app window ----------------------------------------------

    def _build_terminal_window(self) -> None:
        # app frame like XP window on desktop
        self.win = tk.Frame(self.desktop, bg=XP["window_border"], bd=2, relief="solid")
        self.desktop.create_window(220, 90, anchor="nw", window=self.win, width=900, height=560)

        tbar = tk.Frame(self.win, bg=XP["title_top"], height=26)
        tbar.pack(fill="x")
        tbar.pack_propagate(False)
        tc = tk.Canvas(tbar, highlightthickness=0, bd=0, height=26)
        tc.place(x=0, y=0, relwidth=1, relheight=1)
        tc.bind("<Configure>", lambda e: paint_vgradient(tc, XP["title_top"], XP["title_mid"], XP["title_bot"]))
        tk.Label(tbar, text="UNO OS Terminal", fg=XP["title_fg"], bg=XP["title_top"], font=FONT_TITLE).pack(side="left", padx=8)
        tk.Button(tbar, text="X", width=3, command=self._close, bg="#d9534f", fg="white",
                  relief="raised", bd=1, font=FONT_UI_B).pack(side="right", padx=4, pady=3)

        body = tk.Frame(self.win, bg=XP["window_bg"])
        body.pack(fill="both", expand=True)

        top = tk.Frame(body, bg=XP["window_bg"])
        top.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(top, text="Port:", bg=XP["window_bg"], font=FONT_UI).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=10, state="readonly")
        self.port_cb.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh", style="XP.TButton", command=self._refresh_ports).pack(side="left")
        self.btn_connect = ttk.Button(top, text="Connect", style="XP.TButton", command=self._toggle_connect)
        self.btn_connect.pack(side="left", padx=6)
        for text, cmd in [("ls", "ls"), ("df", "df"), ("mem", "mem"), ("help", "help"), ("ver", "ver")]:
            ttk.Button(top, text=text, style="XP.TButton", command=lambda c=cmd: self._send_cmd(c)).pack(side="left", padx=1)
        ttk.Button(top, text="Upload…", style="XP.TButton", command=self._upload_dlg).pack(side="right", padx=2)
        ttk.Button(top, text="Download…", style="XP.TButton", command=self._download_dlg).pack(side="right", padx=2)
        ttk.Button(top, text="Run…", style="XP.TButton", command=self._run_dlg).pack(side="right", padx=2)
        ttk.Button(top, text="Files", style="XP.TButton", command=self.open_file_manager).pack(side="right", padx=2)
        ttk.Button(top, text="Editor", style="XP.TButton", command=self.open_script_editor).pack(side="right", padx=2)
        ttk.Button(top, text="Device", style="XP.TButton", command=self.open_device_panel).pack(side="right", padx=2)

        term_wrap = tk.Frame(body, bg="#777", padx=1, pady=1)
        term_wrap.pack(fill="both", expand=True, padx=8, pady=4)
        self.term = scrolledtext.ScrolledText(term_wrap, font=FONT_MONO, bg=XP["term_bg"], fg=XP["term_fg"],
                                              insertbackground=XP["term_fg"], relief="flat", bd=0, wrap="char")
        self.term.pack(fill="both", expand=True)
        self.term.tag_configure("info", foreground=XP["term_info"])
        self.term.tag_configure("err", foreground=XP["term_err"])
        self.term.configure(state="disabled")
        self._append("Microsoft Windows XP [Version 5.1.2600]\n", "info")
        self._append("(C) UNO OS desktop simulation\n\n", "info")

        inp = tk.Frame(body, bg=XP["window_bg"])
        inp.pack(fill="x", padx=8, pady=(0, 8))
        tk.Label(inp, text="C:\\UNO>", bg=XP["window_bg"], font=FONT_MONO).pack(side="left")
        self.input = ttk.Entry(inp, font=FONT_MONO)
        self.input.pack(side="left", fill="x", expand=True, padx=6)
        self.input.bind("<Return>", lambda _e: self._send_input())
        self.input.bind("<Up>", self._hist_up)
        self.input.bind("<Down>", self._hist_down)
        ttk.Button(inp, text="Send", style="XP.TButton", command=self._send_input).pack(side="left")

        self.status_label = tk.Label(body, text="Ready - disconnected", bg="#d7d3c2", anchor="w", font=FONT_UI)
        self.status_label.pack(fill="x", side="bottom")

    def _close(self) -> None:
        try:
            if self.bridge:
                self.bridge.close()
        finally:
            self.root.destroy()

    # ---- taskbar -----------------------------------------------------------

    def _build_taskbar(self) -> None:
        bar = tk.Frame(self.root, height=32, bg=XP["task_top"])
        bar.pack_propagate(False)
        bar.pack(side="bottom", fill="x")
        cv = tk.Canvas(bar, height=32, highlightthickness=0, bd=0)
        cv.place(x=0, y=0, relwidth=1, relheight=1)
        cv.bind("<Configure>", lambda e: paint_vgradient(cv, XP["task_top"], XP["task_mid"], XP["task_bot"]))

        self.start_cv = tk.Canvas(bar, width=98, height=28, highlightthickness=0, bd=0, bg=XP["task_top"])
        self.start_cv.place(x=2, y=2)
        self._draw_start_button(False)
        self.start_cv.bind("<Enter>", lambda e: self._draw_start_button(True))
        self.start_cv.bind("<Leave>", lambda e: self._draw_start_button(False))
        self.start_cv.bind("<Button-1>", lambda e: self._show_start_menu())
        tk.Label(bar, text="  UNO OS Terminal", bg="#1F53C2", fg="white",
                 font=FONT_UI, anchor="w", padx=8, bd=1, relief="sunken").place(x=108, y=4, width=180, height=24)
        self.clock_lbl = tk.Label(bar, text="--:--", bg="#0E5DD4", fg="white", font=FONT_UI_B, padx=8)
        self.clock_lbl.place(relx=1, x=-92, y=4, width=84, height=24)

    def _draw_start_button(self, hover: bool) -> None:
        c = self.start_cv
        c.delete("all")
        top = XP["start_hover"] if hover else XP["start_top"]
        mid = XP["start_top"] if hover else XP["start_mid"]
        bot = XP["start_mid"] if hover else XP["start_bot"]
        # pill-форма: два круга по бокам + прямоугольник в центре
        c.create_arc(0,   2, 28, 28, start=90,  extent=180, fill=top, outline="")
        c.create_arc(70,  2, 98, 28, start=270, extent=180, fill=top, outline="")
        c.create_rectangle(14, 2, 84, 15, fill=top, outline="")
        # нижняя половинка темнее (фейк-градиент)
        c.create_arc(0,   2, 28, 28, start=180, extent=180, fill=bot, outline="")
        c.create_arc(70,  2, 98, 28, start=180, extent=180, fill=bot, outline="")
        c.create_rectangle(14, 15, 84, 28, fill=bot, outline="")
        c.create_rectangle(14, 13, 84, 17, fill=mid, outline="")
        # 4-цветная "флажок"-иконка
        c.create_rectangle(8,  8, 14, 14, fill="#FF4040", outline="")
        c.create_rectangle(15, 8, 21, 14, fill="#5BAA39", outline="")
        c.create_rectangle(8,  15, 14, 21, fill="#3870D0", outline="")
        c.create_rectangle(15, 15, 21, 21, fill="#FFCC00", outline="")
        # текст
        c.create_text(59, 15, text="start", fill="white", font=FONT_START)

    def _show_start_menu(self) -> None:
        if self.start_menu and self.start_menu.winfo_exists():
            self.start_menu.destroy()
            self.start_menu = None
            return

        m = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.configure(bg="#2E5FB5")
        w, h = 360, 420
        x = self.root.winfo_x() + 2
        y = self.root.winfo_y() + self.root.winfo_height() - 32 - h
        m.geometry(f"{w}x{h}+{x}+{max(0,y)}")
        self.start_menu = m

        header = tk.Frame(m, bg="#2E5FB5", height=64)
        header.pack(fill="x")
        tk.Label(header, text="My UNO", bg="#2E5FB5", fg="white", font=("Tahoma", 14, "bold")).pack(anchor="w", padx=14, pady=16)

        body = tk.Frame(m, bg="#dce8fb")
        body.pack(fill="both", expand=True, padx=2, pady=(0,2))
        left = tk.Frame(body, bg="#f6f8fc")
        left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(body, bg="#dce8fb", width=145)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        for txt, cmd in [
            ("Connect", self._connect),
            ("Disconnect", self._disconnect),
            ("Upload file", self._upload_dlg),
            ("Download file", self._download_dlg),
            ("Run script", self._run_dlg),
            ("Quick edit", self._edit_dlg),
            ("File manager", self.open_file_manager),
            ("Script editor", self.open_script_editor),
            ("Device panel", self.open_device_panel),
            ("Help", lambda: self._send_cmd("help")),
            ("About", self._show_about),
        ]:
            b = tk.Button(left, text=txt, anchor="w", relief="flat", bd=0, bg="#f6f8fc",
                          activebackground="#316ac5", activeforeground="white", font=FONT_UI_B, command=lambda c=cmd: (self._close_start_menu(), c()))
            b.pack(fill="x", padx=8, pady=2, ipady=4)
        for txt, cmd in [("My Computer", self._focus_terminal), ("Control Panel", self._focus_terminal),
                         ("Network Places", self._focus_terminal), ("Search", self._focus_terminal)]:
            b = tk.Button(right, text=txt, anchor="w", relief="flat", bd=0, bg="#dce8fb",
                          activebackground="#316ac5", activeforeground="white", font=FONT_UI, command=lambda c=cmd: (self._close_start_menu(), c()))
            b.pack(fill="x", padx=8, pady=2, ipady=2)

        footer = tk.Frame(m, bg="#2E5FB5", height=40)
        footer.pack(fill="x", side="bottom")
        tk.Button(footer, text="Log Off", bg="#e8edf7", font=FONT_UI, command=self._close_start_menu).pack(side="left", padx=12, pady=7)
        tk.Button(footer, text="Shut Down", bg="#f2d7d5", font=FONT_UI, command=self._close).pack(side="right", padx=12, pady=7)

    def _tick_clock(self) -> None:
        now = time.strftime("%H:%M")
        try:
            self.clock_lbl.configure(text=now)
        except tk.TclError:
            return
        self.root.after(20_000, self._tick_clock)

    # ---- helpers -----------------------------------------------------------

    def _focus_terminal(self) -> None:
        self.win.lift()
        self.input.focus_set()

    def _close_start_menu(self) -> None:
        if self.start_menu and self.start_menu.winfo_exists():
            self.start_menu.destroy()
        self.start_menu = None

    def _click_anywhere(self, _event) -> None:
        self._close_start_menu()

    # ---- порты / connect ---------------------------------------------------

    def _refresh_ports(self) -> None:
        ports = [p.device for p in list_ports.comports()]
        if not ports:
            ports = ["(нет портов)"]
        self.port_cb["values"] = ports
        auto = detect_port()
        if auto and auto in ports:
            self.port_var.set(auto)
        elif self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def _toggle_connect(self) -> None:
        if self.bridge is None:
            self._connect()
        else:
            self._disconnect()

    def _connect(self) -> None:
        port = self.port_var.get()
        if not port or port == "(нет портов)":
            messagebox.showerror("Нет порта", "Выбери COM-порт.")
            return
        try:
            self.bridge = GuiBridge(port, 115200, self._on_data_threadsafe)
        except serial.SerialException as e:
            msg = str(e)
            self._log(f"[!] не удалось открыть {port}: {msg}", "err")
            if "Permission" in msg or "Отказано" in msg or "Access" in msg:
                self._log(
                    "[?] порт занят: закрой Arduino IDE Serial Monitor / "
                    "PuTTY / другой unoctl",
                    "err",
                )
            return
        self._log(f"[i] подключено: {port} @ 115200", "info")
        self.btn_connect.configure(text="Disconnect")
        self.status_label.configure(text=f"Connected to {port} @ 115200")

    def _disconnect(self) -> None:
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        self.btn_connect.configure(text="Connect")
        self.status_label.configure(text="Ready - disconnected")
        self._log("[i] отключено", "info")

    # ---- терминал I/O ------------------------------------------------------

    def _on_data_threadsafe(self, data: bytes) -> None:
        text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "")
        self.root.after(0, lambda: self._append(text))

    def _append(self, text: str, tag: str | None = None) -> None:
        if not text:
            return
        self.term.configure(state="normal")
        if tag:
            self.term.insert("end", text, tag)
        else:
            self.term.insert("end", text)
        self.term.see("end")
        self.term.configure(state="disabled")

    def _log(self, text: str, tag: str = "info") -> None:
        if not text.endswith("\n"):
            text += "\n"
        self._append(text, tag)

    def _clear_term(self) -> None:
        self.term.configure(state="normal")
        self.term.delete("1.0", "end")
        self.term.configure(state="disabled")

    def _send_cmd(self, cmd: str) -> None:
        if not self.bridge:
            self._log("[!] не подключено", "err")
            return
        try:
            self.bridge.write((cmd + "\n").encode("utf-8"))
        except Exception as e:
            self._log(f"[!] write failed: {e}", "err")

    def _send_input(self) -> None:
        if not self.bridge:
            self._log("[!] не подключено", "err")
            return
        s = self.input.get()
        self.input.delete(0, "end")
        if s:
            self.history.append(s)
            self.history_idx = len(self.history)
        try:
            self.bridge.write((s + "\n").encode("utf-8"))
        except Exception as e:
            self._log(f"[!] write failed: {e}", "err")

    def _send_ctrl_c(self, _evt) -> str:
        if self.bridge:
            try:
                self.bridge.write(b"\x03")
            except Exception:
                pass
        return "break"

    def _hist_up(self, _evt) -> str:
        if not self.history:
            return "break"
        self.history_idx = max(0, self.history_idx - 1)
        self.input.delete(0, "end")
        self.input.insert(0, self.history[self.history_idx])
        return "break"

    def _hist_down(self, _evt) -> str:
        if not self.history:
            return "break"
        self.history_idx = min(len(self.history), self.history_idx + 1)
        self.input.delete(0, "end")
        if self.history_idx < len(self.history):
            self.input.insert(0, self.history[self.history_idx])
        return "break"

    # ---- file ops ---------------------------------------------------------

    def _need_bridge(self) -> bool:
        if self.bridge is None:
            messagebox.showerror("Нет подключения", "Сначала Connect.")
            return False
        return True

    def _upload_dlg(self) -> None:
        if not self._need_bridge():
            return
        local = filedialog.askopenfilename(
            title="Файл для заливки на UNO",
            filetypes=[("Python script", "*.py"), ("Text", "*.txt"),
                       ("Все файлы", "*.*")],
        )
        if not local:
            return
        default = os.path.basename(local)
        if "." in default:
            stem, _, _ = default.rpartition(".")
            default = (stem or default)[:8]
        else:
            default = default[:8]
        remote = simpledialog.askstring(
            "Имя на UNO", "Имя файла на UNO (≤ 8 символов):",
            initialvalue=default, parent=self.root,
        )
        if not remote:
            return
        threading.Thread(target=self._do_upload,
                         args=(local, remote), daemon=True).start()

    def _do_upload(self, local: str, remote: str) -> None:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                upload(self.bridge, local, remote)
        except Exception as e:
            self.root.after(0, lambda: self._log(f"[!] upload failed: {e}", "err"))
            return
        text = buf.getvalue()
        self.root.after(0, lambda: self._append(text, "info"))

    def _download_dlg(self) -> None:
        if not self._need_bridge():
            return
        remote = simpledialog.askstring(
            "Скачать с UNO", "Имя файла на UNO:", parent=self.root,
        )
        if not remote:
            return
        local = filedialog.asksaveasfilename(
            title="Куда сохранить", initialfile=remote, defaultextension=".py",
        )
        if not local:
            return
        threading.Thread(target=self._do_download,
                         args=(remote, local), daemon=True).start()

    def _do_download(self, remote: str, local: str) -> None:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                download(self.bridge, remote, local)
        except Exception as e:
            self.root.after(0, lambda: self._log(f"[!] download failed: {e}", "err"))
            return
        text = buf.getvalue()
        self.root.after(0, lambda: self._append(text, "info"))

    def _run_dlg(self) -> None:
        if not self._need_bridge():
            return
        name = simpledialog.askstring("Запустить скрипт",
                                      "Имя скрипта на UNO:",
                                      parent=self.root)
        if name:
            self._send_cmd(f"py {name}")

    def _edit_dlg(self) -> None:
        if not self._need_bridge():
            return
        EditDialog(self.root, self)

    def _reboot(self) -> None:
        if not self._need_bridge():
            return
        if messagebox.askyesno("Reboot", "Перезагрузить UNO через WDT?"):
            self._send_cmd("reboot")

    def _show_about(self) -> None:
        d = tk.Toplevel(self.root)
        d.title("About UNO OS")
        d.configure(bg=XP["window_bg"])
        d.geometry(
            f"460x320+{self.root.winfo_x() + 200}+{self.root.winfo_y() + 150}"
        )
        d.resizable(False, False)
        d.transient(self.root)
        try:
            d.grab_set()
        except tk.TclError:
            pass

        tk.Label(d, text="UNO OS v0.1", bg=XP["window_bg"], font=("Tahoma", 14, "bold"),
                 fg="#1b4f9f").pack(pady=(20, 5))
        tk.Label(d, text="A tiny operating system for Arduino UNO",
                 bg=XP["window_bg"], font=FONT_UI).pack()
        tk.Label(
            d,
            text=(
                "22 shell commands · 1 KB EEPROM file system\n"
                "mini-Python interpreter · autoboot via 'boot'"
            ),
            bg=XP["window_bg"], font=FONT_UI, fg="#444", justify="center",
        ).pack(pady=10)
        tk.Label(
            d,
            text="Windows XP desktop-style GUI client built with Tkinter.",
            bg=XP["window_bg"], font=FONT_UI, fg="#666", justify="center",
        ).pack(pady=8)
        tk.Label(d, text="Use start menu and desktop icons to navigate.",
                 bg=XP["window_bg"], font=FONT_UI, fg="#888").pack(pady=8)
        ttk.Button(d, text="OK", style="XP.TButton",
                   command=d.destroy).pack(pady=10)

    # ---- visual shell apps -------------------------------------------------

    def _capture(self, cmd: str, timeout: float = 3.0) -> str:
        if not self.bridge:
            return ""
        self.bridge.begin_silent()
        try:
            self.bridge.write(b"\n")
            self.bridge.read_until(b"uno> ", 1.0)
            self.bridge.write((cmd + "\n").encode("utf-8"))
            out = self.bridge.read_until(b"uno> ", timeout)
        finally:
            self.bridge.end_silent()
        txt = out.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")
        if "\n" in txt:
            first, rest = txt.split("\n", 1)
            if first.strip().startswith(cmd.split()[0]):
                txt = rest
        if txt.endswith("uno> "):
            txt = txt[:-5]
        return txt.strip("\n")

    def _list_files(self) -> list[tuple[str, int]]:
        out = self._capture("ls")
        items: list[tuple[str, int]] = []
        for ln in out.splitlines():
            if not ln.startswith("  "):
                continue
            p = ln.split()
            if len(p) >= 2 and p[-1].isdigit():
                items.append((p[0], int(p[-1])))
        return items

    def open_file_manager(self) -> None:
        if not self._need_bridge():
            return
        FileManager(self.root, self)

    def open_script_editor(self) -> None:
        if not self._need_bridge():
            return
        ScriptStudio(self.root, self)

    def open_device_panel(self) -> None:
        if not self._need_bridge():
            return
        DevicePanel(self.root, self)

    def run(self) -> None:
        self.root.mainloop()


# === Edit dialog ============================================================

class EditDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: XpApp) -> None:
        super().__init__(parent)
        self.app = app
        self.title("UNO OS — quick edit")
        self.configure(bg=XP["beige"])
        self.geometry("540x420")
        try:
            self.transient(parent)
        except tk.TclError:
            pass

        bar = tk.Frame(self, bg=XP["beige"], pady=4)
        bar.pack(fill="x", padx=6, pady=4)
        tk.Label(bar, text="Имя на UNO (≤ 8):", bg=XP["beige"],
                 font=FONT_UI).pack(side="left")
        self.name = ttk.Entry(bar, width=12, font=FONT_UI)
        self.name.pack(side="left", padx=6)
        self.name.insert(0, "hi.py")
        ttk.Button(bar, text="Save → UNO", style="XP.TButton",
                   command=self._save).pack(side="right")

        sunk = tk.Frame(self, bg=XP["beige_dark"], padx=1, pady=1)
        sunk.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.text = scrolledtext.ScrolledText(
            sunk, font=FONT_MONO, wrap="none",
            bg="white", fg="black", relief="flat", bd=0,
        )
        self.text.pack(fill="both", expand=True)
        self.text.insert(
            "1.0",
            "i = 0\nwhile i < 5:\n  print(i)\n  i = i + 1\n",
        )

    def _save(self) -> None:
        name = self.name.get().strip()
        if not name:
            messagebox.showerror("Имя", "Введите имя файла на UNO.")
            return
        if len(name) > 8:
            messagebox.showerror("Имя", "Имя должно быть ≤ 8 символов.")
            return
        body = self.text.get("1.0", "end").rstrip("\n") + "\n"
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py", prefix="unoctl_edit_")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            threading.Thread(
                target=self.app._do_upload,
                args=(path, name), daemon=True,
            ).start()
        except Exception as e:
            messagebox.showerror("Save", f"Не удалось сохранить: {e}")
            return
        self.destroy()


class FileManager(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: XpApp) -> None:
        super().__init__(parent)
        self.app = app
        self.title("UNO File Manager")
        self.configure(bg=XP["beige"])
        self.geometry("470x390")
        self.transient(parent)

        top = tk.Frame(self, bg=XP["beige"], pady=6)
        top.pack(fill="x", padx=8)
        ttk.Button(top, text="Refresh", style="XP.TButton", command=self.refresh).pack(side="left")
        ttk.Button(top, text="Upload...", style="XP.TButton", command=app._upload_dlg).pack(side="left", padx=2)
        ttk.Button(top, text="Download", style="XP.TButton", command=self.download_sel).pack(side="left", padx=2)
        ttk.Button(top, text="Run", style="XP.TButton", command=self.run_sel).pack(side="left", padx=2)
        ttk.Button(top, text="Delete", style="XP.TButton", command=self.delete_sel).pack(side="left", padx=2)

        self.lst = tk.Listbox(self, font=("Consolas", 10))
        self.lst.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.refresh()

    def refresh(self) -> None:
        self.lst.delete(0, "end")
        for name, size in self.app._list_files():
            self.lst.insert("end", f"{name:<8}  {size:>3} b")

    def _sel_name(self) -> str | None:
        s = self.lst.curselection()
        if not s:
            return None
        return self.lst.get(s[0]).split()[0]

    def run_sel(self) -> None:
        n = self._sel_name()
        if n:
            self.app._send_cmd(f"py {n}")

    def delete_sel(self) -> None:
        n = self._sel_name()
        if n and messagebox.askyesno("Delete", f"Удалить {n}?"):
            self.app._send_cmd(f"rm {n}")
            self.after(280, self.refresh)

    def download_sel(self) -> None:
        n = self._sel_name()
        if not n:
            return
        local = filedialog.asksaveasfilename(initialfile=n, defaultextension=".py")
        if local:
            threading.Thread(target=self.app._do_download, args=(n, local), daemon=True).start()


class ScriptStudio(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: XpApp) -> None:
        super().__init__(parent)
        self.app = app
        self.title("UNO Script Editor")
        self.configure(bg=XP["beige"])
        self.geometry("640x470")
        self.transient(parent)

        top = tk.Frame(self, bg=XP["beige"], pady=6)
        top.pack(fill="x", padx=8)
        tk.Label(top, text="Name:", bg=XP["beige"], font=FONT_UI).pack(side="left")
        self.name = ttk.Entry(top, width=12)
        self.name.pack(side="left", padx=4)
        self.name.insert(0, "script")
        ttk.Button(top, text="Open UNO", style="XP.TButton", command=self.open_uno).pack(side="left", padx=2)
        ttk.Button(top, text="Save UNO", style="XP.TButton", command=self.save_uno).pack(side="left", padx=2)
        ttk.Button(top, text="Run", style="XP.TButton", command=self.run_uno).pack(side="left", padx=2)

        self.text = scrolledtext.ScrolledText(self, font=FONT_MONO, wrap="none")
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.text.insert("1.0", "i = 0\nwhile i < 10:\n  print(i)\n  i = i + 1\n")

    def open_uno(self) -> None:
        n = self.name.get().strip()
        if not n:
            return
        txt = self.app._capture(f"cat {n}")
        if "? not found" in txt:
            messagebox.showerror("Open", f"Файл {n} не найден")
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", txt + "\n")

    def save_uno(self) -> None:
        n = self.name.get().strip()[:8]
        if not n:
            messagebox.showerror("Save", "Нужно имя файла")
            return
        body = self.text.get("1.0", "end").rstrip("\n") + "\n"
        fd, path = tempfile.mkstemp(suffix=".py", prefix="uno_gui_")
        os.close(fd)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        threading.Thread(target=self.app._do_upload, args=(path, n), daemon=True).start()

    def run_uno(self) -> None:
        n = self.name.get().strip()
        if n:
            self.app._send_cmd(f"py {n}")


class DevicePanel(tk.Toplevel):
    def __init__(self, parent: tk.Tk, app: XpApp) -> None:
        super().__init__(parent)
        self.app = app
        self.title("UNO Device Panel")
        self.configure(bg=XP["beige"])
        self.geometry("380x320")
        self.transient(parent)

        tk.Label(self, text="Built-in LED", bg=XP["beige"], font=FONT_UI_B).pack(anchor="w", padx=10, pady=(10, 2))
        r1 = tk.Frame(self, bg=XP["beige"])
        r1.pack(fill="x", padx=10)
        ttk.Button(r1, text="ON", style="XP.TButton", command=lambda: app._send_cmd("led on")).pack(side="left", padx=2)
        ttk.Button(r1, text="OFF", style="XP.TButton", command=lambda: app._send_cmd("led off")).pack(side="left", padx=2)

        tk.Label(self, text="Digital pin", bg=XP["beige"], font=FONT_UI_B).pack(anchor="w", padx=10, pady=(14, 2))
        r2 = tk.Frame(self, bg=XP["beige"])
        r2.pack(fill="x", padx=10)
        self.pin = ttk.Entry(r2, width=6)
        self.pin.insert(0, "13")
        self.pin.pack(side="left")
        ttk.Button(r2, text="HIGH", style="XP.TButton", command=self.high).pack(side="left", padx=2)
        ttk.Button(r2, text="LOW", style="XP.TButton", command=self.low).pack(side="left", padx=2)
        ttk.Button(r2, text="READ", style="XP.TButton", command=self.read).pack(side="left", padx=2)

        tk.Label(self, text="Analog read", bg=XP["beige"], font=FONT_UI_B).pack(anchor="w", padx=10, pady=(14, 2))
        r3 = tk.Frame(self, bg=XP["beige"])
        r3.pack(fill="x", padx=10)
        self.apin = ttk.Entry(r3, width=6)
        self.apin.insert(0, "0")
        self.apin.pack(side="left")
        ttk.Button(r3, text="READ A", style="XP.TButton", command=self.aread).pack(side="left", padx=2)
        ttk.Button(self, text="Reboot", style="XP.TButton", command=app._reboot).pack(anchor="w", padx=10, pady=16)

    def _p(self) -> str:
        return self.pin.get().strip() or "13"

    def high(self) -> None:
        p = self._p()
        self.app._send_cmd(f"pinm {p} 1")
        self.app._send_cmd(f"pinw {p} 1")

    def low(self) -> None:
        p = self._p()
        self.app._send_cmd(f"pinm {p} 1")
        self.app._send_cmd(f"pinw {p} 0")

    def read(self) -> None:
        self.app._send_cmd(f"pinr {self._p()}")

    def aread(self) -> None:
        a = self.apin.get().strip() or "0"
        self.app._send_cmd(f"aread {a}")


def main() -> None:
    XpApp().run()


if __name__ == "__main__":
    main()
