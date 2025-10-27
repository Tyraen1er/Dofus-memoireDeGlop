# -*- coding: utf-8 -*-
# === DPI AWARENESS — DOIT ÊTRE EN TOUT PREMIER ===
import ctypes
if hasattr(ctypes, "windll"):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
# ================================================

import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Tuple, List, Optional, Dict
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageTk, ImageDraw
from pynput import mouse, keyboard
import mss
import ctypes as ct
from ctypes import wintypes
import time
import psutil
import sys
try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

Point = Tuple[float, float]

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

CONFIG = {
    "memory_window_ratio": 0.35,
    "capture_frames": 10,
    "capture_interval": 0.2,
    "animation_interval": 0.2,
    "max_capture_threads": 3,
    "canvas_horizontal_padding": 20,
    "canvas_vertical_padding": 40,
}

# === API Windows ===
if IS_WINDOWS and hasattr(ct, "windll"):
    try:
        user32 = ct.windll.user32
    except AttributeError:
        user32 = None
else:
    user32 = None

if IS_WINDOWS and hasattr(ct, "WINFUNCTYPE"):
    EnumWindowsProc = ct.WINFUNCTYPE(ct.c_bool, wintypes.HWND, wintypes.LPARAM)
else:
    EnumWindowsProc = None

class RECT(ct.Structure):
    _fields_ = [
        ("left", ct.c_long),
        ("top", ct.c_long),
        ("right", ct.c_long),
        ("bottom", ct.c_long)
    ]

def find_window_by_title(partial_title: str):
    if not IS_WINDOWS or user32 is None:
        return None
    hwnd = user32.GetTopWindow(None)
    while hwnd:
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ct.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if partial_title.lower() in buffer.value.lower():
                return hwnd
        hwnd = user32.GetWindow(hwnd, 2)
    return None

def get_window_rect(hwnd):
    if not IS_WINDOWS or user32 is None:
        return None
    rect = RECT()
    if user32.GetWindowRect(hwnd, ct.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None

def get_work_area():
    if IS_WINDOWS and user32 is not None:
        try:
            rect = RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ct.byref(rect), 0):
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    root = tk.Tk()
    root.withdraw()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.destroy()
    return 0, 0, w, h

def enumerate_windows_for_pids(pid_set: set) -> List[Tuple[int, str]]:
    if not IS_WINDOWS or win32process is None or win32gui is None or EnumWindowsProc is None or user32 is None:
        return []
    results: List[Tuple[int, str]] = []

    def _callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid not in pid_set:
            return True
        title = win32gui.GetWindowText(hwnd)
        if title and "release" in title.lower():
            results.append((hwnd, title.strip()))
        return True

    enum_cb = EnumWindowsProc(_callback)
    user32.EnumWindows(enum_cb, 0)
    return results

# ------------------ Grille bilinéaire ------------------
def grid_intersections_in_quad(c1, c2, c3, c4, n, m) -> List[List[Point]]:
    x1, y1 = map(float, c1)
    x2, y2 = map(float, c2)
    x3, y3 = map(float, c3)
    x4, y4 = map(float, c4)
    def bilinear(u: float, v: float) -> Point:
        return (
            (1 - u) * (1 - v) * x1 + u * (1 - v) * x2 + u * v * x3 + (1 - u) * v * x4,
            (1 - u) * (1 - v) * y1 + u * (1 - v) * y2 + u * v * y3 + (1 - u) * v * y4,
        )
    return [[bilinear(i / m, j / n) for i in range(m + 1)] for j in range(n + 1)]

def closest_point_with_indices(grid: List[List[Point]], target: Point):
    tx, ty = target
    best_pt, best_idx, best_d2 = None, (-1, -1), float("inf")
    for j, row in enumerate(grid):
        for i, (px, py) in enumerate(row):
            d2 = (px - tx) ** 2 + (py - ty) ** 2
            if d2 < best_d2:
                best_d2, best_pt, best_idx = d2, (px, py), (j, i)
    return best_pt, best_idx

# ------------------ Application principale ------------------
class QuadGridNodesApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("🧠 Memory Helper — Aide au jeu")
        self.root.wm_attributes("-topmost", True)

        self.mode = "start"
        self.points: List[Tuple[int, int]] = []
        self.default_ratios = [
            (0.5346, 0.2870),
            (0.7786, 0.5023),
            (0.6336, 0.6361),
            (0.3898, 0.4139)
        ]
        self.target_window_title = "Nodon"
        self.target_hwnd: Optional[int] = None
        self._next_point_index = 0
        self._quitting = False

        self.preview_label = None
        self.status = None
        self.n, self.m, self.cell = 3, 5, 200
        self.display_cell = self.cell
        self.tile_items = {}
        self.tile_images = {}
        self.tile_border_items = {}
        self.tile_sequences = {}
        self.tile_animation_index: Dict[Tuple[int, int], int] = {}
        self.animation_job: Optional[str] = None
        self.capture_executor = ThreadPoolExecutor(max_workers=CONFIG["max_capture_threads"])
        self.listener = None
        self.listener_lock = threading.Lock()
        self.click_history: List[Dict[str, object]] = []
        self.click_map_label = None
        self.side_panel = None
        self.main_frame = None
        self.controls_frame = None
        self.selector_var: Optional[tk.StringVar] = None
        self.dofus_entries: List[Dict[str, object]] = []

        self.sct = mss.mss()
        self.vmon = self.sct.monitors[0]
        self.pixel_ratio = self._detect_pixel_ratio()

        self.show_dofus_gate()
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.start_keyboard_listener()
        self.root.mainloop()

    def capture_target_window_image(self) -> bool:
        """Capture la fenêtre cible. Retourne True si la fenêtre Dofus a été capturée."""
        hwnd = self.target_hwnd or find_window_by_title(self.target_window_title)
        if hwnd:
            rect = get_window_rect(hwnd)
            if rect:
                x, y, w, h = rect
                monitor = {"top": y, "left": x, "width": w, "height": h}
                raw = self.sct.grab(monitor)
                self.initial_img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
                self.target_rect = (x, y, w, h)
                self.original_w, self.original_h = w, h
                self.target_hwnd = hwnd
                return True
        # Fallback : écran entier
        raw = self.sct.grab(self.vmon)
        self.initial_img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
        self.target_rect = (self.vmon["left"], self.vmon["top"], self.vmon["width"], self.vmon["height"])
        self.original_w, self.original_h = self.vmon["width"], self.vmon["height"]
        return False

    def load_points_from_ratios(self, ratios):
        x, y, w, h = self.target_rect
        return [(int(x + rx * w), int(y + ry * h)) for (rx, ry) in ratios]

    def _detect_pixel_ratio(self) -> float:
        if not IS_MAC:
            return 1.0
        try:
            logical_w = max(1, int(self.root.winfo_screenwidth()))
            physical_w = int(self.vmon.get("width", logical_w))
            ratio = float(physical_w) / float(logical_w)
            if ratio < 1.0:
                ratio = 1.0
            return ratio
        except Exception:
            return 1.0

    def _logical_to_physical_point(self, point: Tuple[float, float]) -> Tuple[int, int]:
        x, y = point
        if self.pixel_ratio == 1.0:
            return int(round(x)), int(round(y))
        return int(round(x * self.pixel_ratio)), int(round(y * self.pixel_ratio))

    def scan_dofus_windows(self) -> List[Dict[str, object]]:
        entries: List[Dict[str, object]] = []
        if win32gui is None or win32process is None:
            return entries
        pid_set = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info["name"] or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name == "dofus.exe":
                pid_set.add(proc.info["pid"])
                for child in proc.children(recursive=True):
                    pid_set.add(child.pid)
        if not pid_set:
            return entries
        seen = set()
        for hwnd, title in enumerate_windows_for_pids(pid_set):
            if hwnd in seen:
                continue
            seen.add(hwnd)
            rect = get_window_rect(hwnd)
            if not rect:
                continue
            entries.append({
                "hwnd": hwnd,
                "title": title,
                "rect": rect,
                "label": f"{title} — 0x{hwnd:08X}"
            })
        entries.sort(key=lambda e: e["title"].lower())
        return entries

    def show_dofus_gate(self):
        self.mode = "gate"
        for widget in self.root.winfo_children():
            widget.destroy()
        gate_frame = tk.Frame(self.root, padx=20, pady=20)
        gate_frame.pack(fill="both", expand=True)

        if win32gui is None or win32process is None:
            tk.Label(
                gate_frame,
                text="PyWin32 est requis pour détecter Dofus.",
                font=("Arial", 12, "bold")
            ).pack(pady=10)
            tk.Label(
                gate_frame,
                text="Installez pywin32 puis relancez le programme.",
                font=("Arial", 10)
            ).pack(pady=5)
            tk.Button(gate_frame, text="Fermer", command=self.on_quit).pack(pady=15)
            return

        self.dofus_entries = self.scan_dofus_windows()
        if self.dofus_entries:
            tk.Label(
                gate_frame,
                text="Fenêtres Dofus détectées (Release)",
                font=("Arial", 12, "bold")
            ).pack(pady=(0, 10))
            self.selector_var = tk.StringVar(value=self.dofus_entries[0]["label"])
            combo = ttk.Combobox(
                gate_frame,
                textvariable=self.selector_var,
                state="readonly",
                values=[entry["label"] for entry in self.dofus_entries]
            )
            combo.pack(fill="x", padx=10, pady=5)

            btn_frame = tk.Frame(gate_frame)
            btn_frame.pack(pady=15)
            tk.Button(btn_frame, text="Valider", command=self.on_validate_dofus_selection).pack(side="left", padx=10)
            tk.Button(btn_frame, text="Fermer", command=self.on_quit).pack(side="right", padx=10)
        else:
            tk.Label(
                gate_frame,
                text="Aucune fenêtre Dofus 'Release' détectée.",
                font=("Arial", 12, "bold")
            ).pack(pady=10)
            tk.Label(
                gate_frame,
                text="Connectez-vous à Dofus puis cliquez sur Réessayer.",
                font=("Arial", 10)
            ).pack(pady=5)
            btn_frame = tk.Frame(gate_frame)
            btn_frame.pack(pady=15)
            tk.Button(btn_frame, text="Réessayer", command=self.show_dofus_gate).pack(side="left", padx=10)
            tk.Button(btn_frame, text="Fermer", command=self.on_quit).pack(side="right", padx=10)

    def on_validate_dofus_selection(self):
        if not self.dofus_entries or self.selector_var is None:
            return
        label = self.selector_var.get()
        entry = next((e for e in self.dofus_entries if e["label"] == label), None)
        if not entry:
            messagebox.showwarning("Sélection", "Veuillez choisir une fenêtre valide.")
            return
        self.target_hwnd = entry["hwnd"]
        self.target_window_title = entry["title"]
        if not self.capture_target_window_image():
            messagebox.showerror("Capture", "Impossible de capturer la fenêtre sélectionnée.")
            self.show_dofus_gate()
            return
        self.points = self.load_points_from_ratios(self.default_ratios)
        self.setup_start_ui()

    def setup_start_ui(self):
        # Nettoyer
        for widget in self.root.winfo_children():
            widget.destroy()

        # Calculer la taille de l'aperçu (25%)
        scale = 0.25
        preview_w = int(self.original_w * scale)
        preview_h = int(self.original_h * scale)

        # Créer un canvas ou label avec taille fixe pour éviter le noir
        self.preview_frame = tk.Frame(self.root, width=preview_w, height=preview_h, bg="black")
        self.preview_frame.pack(pady=10)
        self.preview_frame.pack_propagate(False)  # Garde la taille même si vide

        self.preview_label = tk.Label(self.preview_frame, bg="black")
        self.preview_label.place(relx=0.5, rely=0.5, anchor="center")

        # Boutons de choix
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=10)
        tk.Button(
            btn_frame, text="✅ Utiliser configuration par défaut",
            command=self.use_default_config, font=("Arial", 10, "bold")
        ).pack(side="left", padx=10)
        tk.Button(
            btn_frame, text="🔧 Configurer les 4 points",
            command=self.enter_config_mode, font=("Arial", 10)
        ).pack(side="right", padx=10)

        self.status = tk.Label(
            self.root,
            text=f"Cible : '{self.target_window_title}'. Aperçu à 25%.",
            font=("Arial", 10)
        )
        self.status.pack(fill="x", pady=5)

        # Charger les points par défaut
        self.points = self.load_points_from_ratios(self.default_ratios)

        # Afficher l'aperçu APRÈS que l'UI soit prête
        self.root.after(50, self._update_preview_image)
        self.root.after(150, self._place_config_window)


    def _update_preview_image(self):
        if not hasattr(self, 'initial_img') or self.initial_img is None:
            return

        scale = 0.25
        img_w, img_h = self.initial_img.size
        new_w = int(img_w * scale)
        new_h = int(img_h * scale)
        if new_w <= 0 or new_h <= 0:
            return

        resized = self.initial_img.copy().resize((new_w, new_h), Image.LANCZOS)
        draw = ImageDraw.Draw(resized)

        # Convertir les points ABSOLUS en coordonnées RELATIVES à la fenêtre cible
        x0, y0, _, _ = self.target_rect
        relative_points = [(px - x0, py - y0) for (px, py) in self.points]

        # Mettre à l'échelle pour l'aperçu
        scaled_pts = [(int(rx * scale), int(ry * scale)) for (rx, ry) in relative_points]

        for i, (x, y) in enumerate(scaled_pts, 1):
            r = 4
            draw.ellipse((x - r, y - r, x + r, y + r), fill="yellow", outline="red", width=2)
            draw.text((x + r, y - r), str(i), fill="white")
        if len(scaled_pts) == 4:
            draw.polygon(scaled_pts, outline="red", width=2)

        tk_img = ImageTk.PhotoImage(resized)
        self.preview_label.config(image=tk_img)
        self.preview_label.image = tk_img  # keep reference

    def _place_config_window(self):
        _, _, work_w, work_h = get_work_area()
        self.root.update_idletasks()
        win_w = self.root.winfo_width()
        win_h = self.root.winfo_height()
        # S'assurer qu'on ne dépasse pas
        if win_w > work_w:
            win_w = work_w
        if win_h > work_h:
            win_h = work_h
        x = 0
        y = work_h - win_h
        if y < 0:
            y = 0
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")

    def _memory_window_limits(self):
        _, _, work_w, work_h = get_work_area()
        ratio = CONFIG["memory_window_ratio"]
        max_w = max(1, int(work_w * ratio))
        max_h = max(1, int(work_h * ratio))
        return max_w, max_h

    def _place_memory_window(self):
        _, _, work_w, work_h = get_work_area()
        win_w, win_h = self._memory_window_limits()
        x = 0
        y = work_h - win_h
        if y < 0: y = 0
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")

    def use_default_config(self):
        self.points = self.load_points_from_ratios(self.default_ratios)
        self._enter_capture_mode()

    def enter_config_mode(self):
        self.mode = "config"
        self._next_point_index = 0
        self.points = self.load_points_from_ratios(self.default_ratios)
        self.status.config(text="Appuyez sur ESPACE ×4 pour redéfinir les coins.")

        for widget in self.root.winfo_children():
            widget.destroy()

        self.preview_label = tk.Label(self.root, bg="black")
        self.preview_label.pack(fill="both", expand=True)

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="✅ Confirmer", command=self.confirm_config, font=("Arial", 10, "bold")).pack(side="left", padx=10)
        tk.Button(btn_frame, text="🔄 Recharger par défaut", command=self.reload_default_for_config, font=("Arial", 10)).pack(side="right", padx=10)

        self.status = tk.Label(self.root, text="Appuyez sur ESPACE ×4...", font=("Arial", 10))
        self.status.pack(fill="x", pady=5)

        self._update_preview_image()
        self.root.after(100, self._place_config_window)

    def reload_default_for_config(self):
        self.points = self.load_points_from_ratios(self.default_ratios)
        self._next_point_index = 0
        self.status.config(text="Configuration réinitialisée. Appuyez sur ESPACE ×4.")
        self._update_preview_image()

    def confirm_config(self):
        if len(self.points) != 4:
            messagebox.showwarning("Erreur", "4 points requis.")
            return
        self._enter_capture_mode()

    def _enter_capture_mode(self):
        self.mode = "capture"
        for widget in self.root.winfo_children():
            widget.destroy()

        top = tk.Frame(self.root)
        self.controls_frame = top
        top.pack(fill="x")
        tk.Label(top, text="n:", font=("Arial", 12, "bold")).pack(side="left")
        self.n_var = tk.StringVar(value=str(self.n))
        tk.Entry(top, textvariable=self.n_var, width=4).pack(side="left", padx=5)
        tk.Label(top, text="m:", font=("Arial", 12, "bold")).pack(side="left")
        self.m_var = tk.StringVar(value=str(self.m))
        tk.Entry(top, textvariable=self.m_var, width=4).pack(side="left", padx=5)
        tk.Label(top, text="Taille(px):", font=("Arial", 12, "bold")).pack(side="left")
        self.cell_var = tk.StringVar(value=str(self.cell))
        tk.Entry(top, textvariable=self.cell_var, width=6).pack(side="left", padx=5)
        tk.Button(top, text="Réinitialiser (R)", command=self.reset, font=("Arial", 10, "bold")).pack(side="right", padx=8, pady=4)

        self.status = tk.Label(self.root, text="✅ Mode capture activé.", font=("Arial", 11))
        self.status.pack(fill="x", pady=3)
        self.click_history.clear()
        if self.main_frame:
            self.main_frame.destroy()
        self.main_frame = tk.Frame(self.root)
        self.main_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(self.main_frame, bg="#111", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.side_panel = tk.Frame(self.main_frame, width=260, bg="#1b1b1b")
        self.side_panel.pack(side="right", fill="y")
        self.side_panel.pack_propagate(False)
        self._build_side_panel()

        self.read_params()
        c1, c2, c3, c4 = self.points
        self.grid = grid_intersections_in_quad(c1, c2, c3, c4, self.n, self.m)
        self.update_canvas_size()
        self.start_global_listener()
        self.root.after(100, self._place_memory_window)

    def _build_side_panel(self):
        if not self.side_panel:
            return
        for child in self.side_panel.winfo_children():
            child.destroy()
        tk.Label(
            self.side_panel,
            text="Aperçu des zones cliquées",
            fg="#f2f2f2",
            bg="#1b1b1b",
            font=("Arial", 11, "bold")
        ).pack(anchor="w", padx=8, pady=(10, 4))

        self.click_map_label = tk.Label(
            self.side_panel,
            bg="#1b1b1b",
            fg="#dddddd",
            text="Aucun clic pour l'instant",
            wraplength=210,
            justify="center"
        )
        self.click_map_label.pack(fill="x", padx=8, pady=(4, 10))

    def clear_click_history(self):
        self.click_history.clear()
        if self.click_map_label:
            self.click_map_label.config(image="", text="Aucun clic pour l'instant")
            self.click_map_label.image = None

    def update_click_map_preview(self):
        if not self.click_map_label or not hasattr(self, "initial_img") or self.initial_img is None:
            return
        if not self.click_history:
            self.click_map_label.config(image="", text="Aucun clic pour l'instant")
            self.click_map_label.image = None
            return

        base = self.initial_img.copy()
        draw = ImageDraw.Draw(base)
        colors = ["#ff5252", "#ffa502", "#2ed573", "#1e90ff", "#a29bfe"]
        for idx, snapshot in enumerate(self.click_history, 1):
            rx, ry = snapshot.get("relative_point", (0, 0))
            color = colors[(idx - 1) % len(colors)]
            r = max(6, self.original_w // 80)
            draw.ellipse((rx - r, ry - r, rx + r, ry + r), outline=color, width=3)
            draw.text((rx + r + 2, ry - r), str(idx), fill=color)

        scale = min(0.4, max(0.15, 260 / max(self.original_w, self.original_h)))
        new_w = max(1, int(self.original_w * scale))
        new_h = max(1, int(self.original_h * scale))
        preview = base.resize((new_w, new_h), Image.LANCZOS)
        tk_img = ImageTk.PhotoImage(preview)
        self.click_map_label.config(image=tk_img, text="")
        self.click_map_label.image = tk_img

    def read_params(self):
        try: self.n = max(1, int(self.n_var.get()))
        except: self.n = 3
        try: self.m = max(1, int(self.m_var.get()))
        except: self.m = 5
        try: self.cell = max(10, int(self.cell_var.get()))
        except: self.cell = 200

    def update_canvas_size(self):
        """Redimensionne l'aire d'affichage pour rester dans la limite de 35 % de l'écran."""
        if not self.canvas:
            return
        self.read_params()
        self.root.update_idletasks()
        max_win_w, max_win_h = self._memory_window_limits()

        side_panel_w = 0
        if self.side_panel:
            side_panel_w = max(self.side_panel.winfo_width(), self.side_panel.winfo_reqwidth())
        controls_h = 0
        if self.controls_frame:
            controls_h = max(self.controls_frame.winfo_height(), self.controls_frame.winfo_reqheight())
        status_h = 0
        if self.status:
            status_h = max(self.status.winfo_height(), self.status.winfo_reqheight())

        horizontal_padding = CONFIG["canvas_horizontal_padding"]
        vertical_padding = CONFIG["canvas_vertical_padding"]
        available_w = max(1, max_win_w - side_panel_w - horizontal_padding)
        available_h = max(1, max_win_h - controls_h - status_h - vertical_padding)

        width_based = max(1, available_w // max(1, (self.m + 1)))
        height_based = max(1, available_h // max(1, (self.n + 1)))
        self.display_cell = max(1, min(self.cell, width_based, height_based))

        canvas_w = self.display_cell * (self.m + 1)
        canvas_h = self.display_cell * (self.n + 1)
        self.canvas.config(width=canvas_w, height=canvas_h)
        self.canvas.configure(scrollregion=(0, 0, canvas_w, canvas_h))

    def on_space(self, event=None):
        if self.mode != "config" or self._next_point_index >= 4:
            return
        coords = self._logical_to_physical_point((self.root.winfo_pointerx(), self.root.winfo_pointery()))
        self.points[self._next_point_index] = coords
        self._next_point_index += 1
        self._update_preview_image()
        if self._next_point_index < 4:
            self.status.config(text=f"Coin {self._next_point_index} défini. Encore {4 - self._next_point_index} × ESPACE…")
        else:
            self.status.config(text="✅ 4 coins définis. Cliquez sur Confirmer.")

    def reset(self):
        self.clear_click_history()
        self._stop_animation_loop()
        self.tile_sequences.clear()
        for d in (self.tile_items, self.tile_border_items):
            for item in list(d.values()):
                self.canvas.delete(item)
            d.clear()
        self.tile_images.clear()
        if self.status:
            self.status.config(text="Snapshots effacés.")

    def start_keyboard_listener(self):
        def on_press(key):
            try:
                if key == keyboard.Key.esc:
                    self.on_quit()
                elif key == keyboard.Key.space:
                    self.on_space()
                elif hasattr(key, "char") and key.char and key.char.lower() == "r":
                    if self.mode == "capture":
                        self.reset()
            except: pass
        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        self.kb_listener = listener

    def on_quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.stop_global_listener()
        self._stop_animation_loop()
        if hasattr(self, "capture_executor") and self.capture_executor is not None:
            try:
                self.capture_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.capture_executor.shutdown(wait=False)
            self.capture_executor = None
        try: self.kb_listener.stop()
        except: pass
        self.root.destroy()

    def start_global_listener(self):
        with self.listener_lock:
            if self.listener: return
            self.listener = mouse.Listener(on_click=self.on_global_click)
            self.listener.daemon = True
            self.listener.start()

    def stop_global_listener(self):
        with self.listener_lock:
            if self.listener:
                try: self.listener.stop()
                except: pass
                self.listener = None

    def on_global_click(self, x, y, button, pressed):
        if not pressed or str(button) != "Button.left" or self.grid is None:
            return
        self.root.after(200, lambda: self.update_tile_from_intersection(x, y))

    def update_tile_from_intersection(self, sx, sy):
        if self.grid is None:
            return
        sx, sy = self._logical_to_physical_point((sx, sy))
        (px, py), (j, i) = closest_point_with_indices(self.grid, (sx, sy))
        self.read_params()
        half = self.cell // 2
        monitor = {
            "left": int(px - half),
            "top": int(py - half),
            "width": self.cell,
            "height": self.cell
        }
        coord = (j, i)
        if self.status:
            self.status.config(text=f"Capture en cours pour ({j},{i})…")
        if not hasattr(self, "capture_executor") or self.capture_executor is None:
            return
        try:
            self.capture_executor.submit(self._capture_sequence_for_tile, coord, monitor, px, py)
        except RuntimeError:
            pass

    def _capture_sequence_for_tile(self, coord, monitor, px, py):
        frames: List[Image.Image] = []
        local_sct = mss.mss()
        try:
            for idx in range(CONFIG["capture_frames"]):
                raw = local_sct.grab(monitor)
                img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
                frames.append(img.copy())
                if idx < CONFIG["capture_frames"] - 1:
                    time.sleep(CONFIG["capture_interval"])
        finally:
            try:
                local_sct.close()
            except Exception:
                pass
        try:
            self.root.after(0, lambda: self._apply_tile_sequence(coord, frames, px, py))
        except tk.TclError:
            return

    def _apply_tile_sequence(self, coord, frames, px, py):
        if not frames or not self.canvas:
            return
        j, i = coord
        display_size = max(1, int(self.display_cell))
        photos = [ImageTk.PhotoImage(frame.resize((display_size, display_size), Image.LANCZOS)) for frame in frames]
        self.tile_sequences[coord] = photos
        self.tile_images[coord] = photos
        self.tile_animation_index[coord] = 0

        cx = i * self.display_cell + self.display_cell // 2
        cy = j * self.display_cell + self.display_cell // 2
        if coord in self.tile_items:
            self.canvas.coords(self.tile_items[coord], cx, cy)
            self.canvas.itemconfig(self.tile_items[coord], image=photos[0])
        else:
            self.tile_items[coord] = self.canvas.create_image(cx, cy, image=photos[0])

        rect_coords = (
            i * self.display_cell, j * self.display_cell,
            (i + 1) * self.display_cell, (j + 1) * self.display_cell,
        )
        if coord in self.tile_border_items:
            self.canvas.coords(self.tile_border_items[coord], *rect_coords)
        else:
            self.tile_border_items[coord] = self.canvas.create_rectangle(*rect_coords, outline="#ff3366", width=2)

        target_rect = getattr(self, "target_rect", (self.vmon["left"], self.vmon["top"], self.vmon["width"], self.vmon["height"]))
        rel_point = (int(px - target_rect[0]), int(py - target_rect[1]))
        snapshot_data = {
            "index": len(self.click_history) + 1,
            "coord": coord,
            "relative_point": rel_point,
            "timestamp": time.strftime("%H:%M:%S"),
            "frames": frames
        }
        self.click_history.append(snapshot_data)
        self.update_click_map_preview()
        if self.status:
            self.status.config(text=f"Série capturée pour ({j},{i})")
        self._ensure_animation_loop()

    def _ensure_animation_loop(self):
        if self.animation_job is not None or not self.root:
            return
        interval_ms = max(10, int(CONFIG["animation_interval"] * 1000))
        self.animation_job = self.root.after(interval_ms, self._animation_loop)

    def _animation_loop(self):
        if not self.canvas:
            self._stop_animation_loop()
            return
        interval_ms = max(10, int(CONFIG["animation_interval"] * 1000))
        active = False
        for coord, frames in list(self.tile_sequences.items()):
            if not frames or coord not in self.tile_items:
                continue
            active = True
            idx = self.tile_animation_index.get(coord, 0) % len(frames)
            self.canvas.itemconfig(self.tile_items[coord], image=frames[idx])
            self.tile_animation_index[coord] = (idx + 1) % len(frames)
        if active:
            try:
                self.animation_job = self.root.after(interval_ms, self._animation_loop)
            except tk.TclError:
                self.animation_job = None
        else:
            self.animation_job = None

    def _stop_animation_loop(self):
        if self.animation_job is not None:
            try:
                self.root.after_cancel(self.animation_job)
            except tk.TclError:
                pass
            self.animation_job = None
        self.tile_animation_index.clear()

if __name__ == "__main__":
    QuadGridNodesApp()
