# -*- coding: utf-8 -*-
# === DPI AWARENESS — DOIT ÊTRE EN TOUT PREMIER ===
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except:
        ctypes.windll.user32.SetProcessDPIAware()
# ================================================

import threading
import tkinter as tk
from tkinter import messagebox
from typing import Tuple, List, Optional, Dict
from PIL import Image, ImageTk, ImageDraw
from pynput import mouse, keyboard
import mss
import ctypes as ct
from ctypes import wintypes

Point = Tuple[float, float]

# === API Windows ===
user32 = ct.windll.user32

class RECT(ct.Structure):
    _fields_ = [
        ("left", ct.c_long),
        ("top", ct.c_long),
        ("right", ct.c_long),
        ("bottom", ct.c_long)
    ]

def find_window_by_title(partial_title: str):
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
    rect = RECT()
    if user32.GetWindowRect(hwnd, ct.byref(rect)):
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    return None

def get_work_area():
    try:
        rect = RECT()
        if user32.SystemParametersInfoW(0x0030, 0, ct.byref(rect), 0):
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except:
        pass
    root = tk.Tk()
    root.withdraw()
    w, h = root.winfo_screenwidth(), root.winfo_screenheight()
    root.destroy()
    return 0, 0, w, h

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
        self.target_window_title = "Nodon"  # ←←← MODIFIEZ ICI
        self._next_point_index = 0
        self._quitting = False

        self.preview_label = None
        self.status = None
        self.n, self.m, self.cell = 3, 5, 200
        self.tile_items = {}
        self.tile_images = {}
        self.tile_border_items = {}
        self.listener = None
        self.listener_lock = threading.Lock()

        self.sct = mss.mss()
        self.vmon = self.sct.monitors[0]
        self.capture_target_window_image()  # 🔥 Capture la fenêtre cible

        self.setup_start_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.start_keyboard_listener()
        self.root.mainloop()

    def capture_target_window_image(self):
        """Capture la fenêtre cible UNE FOIS au démarrage."""
        hwnd = find_window_by_title(self.target_window_title)
        if hwnd:
            rect = get_window_rect(hwnd)
            if rect:
                x, y, w, h = rect
                monitor = {"top": y, "left": x, "width": w, "height": h}
                raw = self.sct.grab(monitor)
                self.initial_img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
                self.target_rect = (x, y, w, h)
                self.original_w, self.original_h = w, h
                return
        # Fallback : écran entier
        raw = self.sct.grab(self.vmon)
        self.initial_img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
        self.target_rect = (self.vmon["left"], self.vmon["top"], self.vmon["width"], self.vmon["height"])
        self.original_w, self.original_h = self.vmon["width"], self.vmon["height"]

    def load_points_from_ratios(self, ratios):
        x, y, w, h = self.target_rect
        return [(int(x + rx * w), int(y + ry * h)) for (rx, ry) in ratios]

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

    def _place_memory_window(self):
        _, _, work_w, work_h = get_work_area()
        self.read_params()
        grid_w = (self.m + 1) * self.cell
        grid_h = (self.n + 1) * self.cell
        win_w = grid_w + 20
        win_h = grid_h + 80
        if win_w > work_w: win_w = work_w
        if win_h > work_h: win_h = work_h
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

        self.canvas = tk.Canvas(self.root, bg="#111", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.read_params()
        c1, c2, c3, c4 = self.points
        self.grid = grid_intersections_in_quad(c1, c2, c3, c4, self.n, self.m)
        self.update_canvas_size()
        self.start_global_listener()
        self.root.after(100, self._place_memory_window)

    def read_params(self):
        try: self.n = max(1, int(self.n_var.get()))
        except: self.n = 3
        try: self.m = max(1, int(self.m_var.get()))
        except: self.m = 5
        try: self.cell = max(10, int(self.cell_var.get()))
        except: self.cell = 200

    def update_canvas_size(self):
        self.read_params()
        w, h = (self.m + 1) * self.cell, (self.n + 1) * self.cell
        self.canvas.config(width=w, height=h)

    def on_space(self, event=None):
        if self.mode != "config" or self._next_point_index >= 4:
            return
        x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        self.points[self._next_point_index] = (x, y)
        self._next_point_index += 1
        self._update_preview_image()
        if self._next_point_index < 4:
            self.status.config(text=f"Coin {self._next_point_index} défini. Encore {4 - self._next_point_index} × ESPACE…")
        else:
            self.status.config(text="✅ 4 coins définis. Cliquez sur Confirmer.")

    def reset(self):
        for d in (self.tile_items, self.tile_border_items):
            for item in list(d.values()):
                self.canvas.delete(item)
            d.clear()
        self.tile_images.clear()
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
        (px, py), (j, i) = closest_point_with_indices(self.grid, (sx, sy))
        self.read_params()
        raw = self.sct.grab(self.vmon)
        img = Image.frombytes("RGB", (raw.width, raw.height), raw.rgb)
        half = self.cell // 2
        lx, ly = int(px - self.vmon["left"]), int(py - self.vmon["top"])
        crop = img.crop((lx - half, ly - half, lx + half, ly + half))
        tk_img = ImageTk.PhotoImage(crop)
        self.tile_images[(j, i)] = tk_img
        cx = i * self.cell + self.cell // 2
        cy = j * self.cell + self.cell // 2
        if (j, i) in self.tile_items:
            self.canvas.itemconfig(self.tile_items[(j, i)], image=tk_img)
        else:
            self.tile_items[(j, i)] = self.canvas.create_image(cx, cy, image=tk_img)
        rect_id = self.canvas.create_rectangle(
            i * self.cell, j * self.cell,
            (i + 1) * self.cell, (j + 1) * self.cell,
            outline="#ff3366", width=2
        )
        if (j, i) in self.tile_border_items:
            self.canvas.delete(self.tile_border_items[(j, i)])
        self.tile_border_items[(j, i)] = rect_id
        self.status.config(text=f"Snapshot pris sur nœud ({j},{i})")

if __name__ == "__main__":
    QuadGridNodesApp()