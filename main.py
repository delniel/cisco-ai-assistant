"""
AI Screen Assistant
===================
Captures a screen region, OCRs it, asks Gemini via OpenRouter,
and clicks the correct answer automatically.

Hotkeys (configurable):
    F8              — drag-select capture region
    CTRL+SHIFT+S    — capture + solve

Requirements: see requirements.txt
"""

import base64
import io
import json
import os
import queue
import re
import sys
import threading
import time
from difflib import SequenceMatcher

import keyboard
import pyautogui
import pytesseract
import customtkinter as ctk
import tkinter as tk
from openai import OpenAI
from PIL import Image

# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Tesseract: set TESSERACT_PATH env var, or rely on auto-detect / Windows default
_TESSERACT_ENV = os.environ.get("TESSERACT_PATH", "")
if _TESSERACT_ENV:
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_ENV
elif sys.platform == "win32":
    _WIN_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_WIN_DEFAULT):
        pytesseract.pytesseract.tesseract_cmd = _WIN_DEFAULT

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Available models — exactly two, nothing else
MODELS = [
    {"id": "google/gemini-2.5-flash-lite", "label": "Flash Lite  (cheaper)"},
    {"id": "google/gemini-2.5-flash",      "label": "Flash  (better quality)"},
]
DEFAULT_MODEL_ID = MODELS[0]["id"]

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".ai_assistant_settings.json")

DEFAULT_HOTKEYS = {
    "select_region": "f8",
    "solve":         "ctrl+shift+s",
}

SCALE_MIN  = 0.9
SCALE_MAX  = 1.3
SCALE_STEP = 0.05
SCALE_DEFAULT = 1.0

# ── Colour palette ─────────────────────────────────────────────────────────────
NEON   = "#00ff9c"
BG     = "#090e09"
BG2    = "#0d130d"
BORDER = "#1a3a1a"
RED    = "#ff4b4b"
YELLOW = "#ffcc00"
BLUE   = "#7ac0ff"
WHITE  = "#ffffff"
DIM    = "#4a6a5a"

# ── Prompt ─────────────────────────────────────────────────────────────────────
SOLVE_PROMPT = """\
You are an expert IT test-solving AI. Answers must be FACTUALLY CORRECT.

TASK:
Analyse the screenshot and OCR text. Select correct answer(s) ONLY from the given options.

KNOWLEDGE:
Use general IT knowledge (hardware, networking, OS, security, certifications).

QUESTION TYPE:
- SINGLE   → exactly one correct answer
- MULTIPLE → more than one correct answer (only if explicitly stated)
- MATCHING → map left-side labels (A, B, C…) to right-side option numbers

RULES:
1. Choose answers that EXACTLY exist in the options shown.
2. Use the ORIGINAL option numbers — never renumber.
3. For SINGLE questions return exactly one answer.
4. Fix obvious OCR noise using context.
5. No explanations. No extra text.

OUTPUT FORMAT:

SINGLE
[NUMBER]: [EXACT OPTION TEXT]

MULTIPLE
[NUMBER]: [EXACT OPTION TEXT]
[NUMBER]: [EXACT OPTION TEXT]

MATCHING
A-[NUMBER]
B-[NUMBER]
C-[NUMBER]
D-[NUMBER]
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

def load_settings() -> dict:
    defaults = {
        "hotkeys":  DEFAULT_HOTKEYS.copy(),
        "api_key":  "",
        "model_id": DEFAULT_MODEL_ID,
        "scale":    SCALE_DEFAULT,
    }
    if not os.path.exists(SETTINGS_PATH):
        return defaults
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    data[k].setdefault(kk, vv)
        # Clamp scale in case settings file has an out-of-range value
        data["scale"] = max(SCALE_MIN, min(SCALE_MAX, float(data["scale"])))
        return data
    except Exception:
        return defaults


def save_settings(settings: dict) -> None:
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  TESSERACT CHECK
# ═══════════════════════════════════════════════════════════════════════════════

def check_tesseract() -> str | None:
    """Return None if Tesseract is reachable, otherwise an error string."""
    try:
        pytesseract.get_tesseract_version()
        return None
    except Exception as exc:
        return str(exc)


# ═══════════════════════════════════════════════════════════════════════════════
#  OCR HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


_OCR_FIXES = [
    (r'\bPCle\b',       "PCIe",   re.IGNORECASE),
    (r'PCl([^a-zA-Z])', r'PCI\1', 0),
    (r'\bOs\s+simm\b',  "SODIMM", re.IGNORECASE),
    (r'\bO\s+om\b',     "DIMM",   re.IGNORECASE),
    (r'\bsopimm\b',     "SODIMM", re.IGNORECASE),
    (r'\bdimn\b',       "DIMM",   re.IGNORECASE),
    (r'\bdinm\b',       "DIMM",   re.IGNORECASE),
    (r'\bsodim\b',      "SODIMM", re.IGNORECASE),
    (r'\bsimn\b',       "SIMM",   re.IGNORECASE),
]

def _normalize(text: str) -> str:
    for pat, rep, fl in _OCR_FIXES:
        text = re.sub(pat, rep, text, flags=fl) if fl else re.sub(pat, rep, text)
    return text


def _clean_kw(word: str) -> str:
    return _normalize(re.sub(r'\W+', '', word).lower())


def _cluster_y(values: list[int], gap: int = 12) -> list[int]:
    if not values:
        return []
    sv = sorted(set(values))
    clusters: list[list[int]] = [[sv[0]]]
    for v in sv[1:]:
        if v - clusters[-1][-1] <= gap:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [c[len(c) // 2] for c in clusters]


def run_ocr(image: Image.Image, region: tuple) -> tuple[dict, str]:
    """
    Single OCR pass. Returns (row_dict, plain_text).
    Result is shared between the AI call and the click engine to avoid
    running Tesseract twice.
    """
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT, lang="eng")

    words: list[dict] = []
    for i in range(len(data["text"])):
        raw = str(data["text"][i]).strip()
        if not raw or int(data["conf"][i]) < 20:
            continue
        ax = data["left"][i] + region[0]
        ay = data["top"][i]  + region[1]
        cy = ay + data["height"][i] // 2
        words.append({
            "raw":  raw,
            "norm": _clean_kw(raw),
            "x":    ax,
            "y":    ay,
            "w":    data["width"][i],
            "h":    data["height"][i],
            "cy":   cy,
        })

    rows: dict[int, list] = {}
    if words:
        for yl in _cluster_y([w["cy"] for w in words], gap=18):
            rw = [w for w in words if abs(w["cy"] - yl) < 18]
            if rw:
                rows[yl] = rw

    plain = _normalize(" ".join(
        str(data["text"][i]) for i in range(len(data["text"]))
        if str(data["text"][i]).strip() and int(data["conf"][i]) >= 20
    ))
    return rows, plain


def _score_row(keys: list[str], row: list[dict]) -> float:
    if not keys:
        return 0.0
    matched = sum(
        1 for k in keys if len(k) >= 3 and any(
            w["norm"] and (k == w["norm"] or _similar(k, w["norm"]) > 0.85)
            for w in row if len(w["norm"]) >= 3
        )
    )
    meaningful = [k for k in keys if len(k) >= 3]
    return matched / len(meaningful) if meaningful else 0.0


def find_click(target: str, rows: dict, region: tuple) -> tuple:
    """
    Return (cx, cy, score).
    cx lands in the left quarter of the matched row to hit radio/checkbox buttons.
    """
    cleaned = re.sub(r'^\d+[:.\s]+', '', target).strip()
    cleaned = re.sub(r'^[A-Za-z]-\s*', '', cleaned).strip()
    if re.match(r'^[A-Za-z]-\d+$', target.strip()):
        return None, None, 0.0

    keys = [_clean_kw(w) for w in cleaned.split() if len(w) >= 3]
    if not keys:
        return None, None, 0.0

    best_score = 0.0
    best_x = best_y = None
    rx, ry, rw, _ = region

    for _, rws in rows.items():
        sc = _score_row(keys, rws)
        if sc > best_score:
            best_score = sc
            top   = min(w["y"] for w in rws)
            bot   = max(w["y"] + w["h"] for w in rws)
            left  = min(w["x"] for w in rws)
            right = max(w["x"] + w["w"] for w in rws)
            best_y = (top + bot) // 2
            best_x = left + (right - left) // 4

    cx = best_x if best_x else rx + int(rw * 0.12)
    return cx, best_y, best_score


# ═══════════════════════════════════════════════════════════════════════════════
#  IMAGE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _encode_image(image: Image.Image, max_width: int = 1200) -> str:
    w, h = image.size
    if w > max_width:
        image = image.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ═══════════════════════════════════════════════════════════════════════════════
#  AI BACKEND
# ═══════════════════════════════════════════════════════════════════════════════

class AIBackend:
    def __init__(self, api_key: str, model_id: str):
        self._client   = OpenAI(base_url=OPENROUTER_BASE, api_key=api_key.strip())
        self._model_id = model_id

    def ask(self, prompt: str, image: Image.Image, ocr_text: str) -> str:
        full_prompt = f"{prompt}\n\nOCR TEXT FROM SCREEN:\n{ocr_text}"
        b64 = _encode_image(image)
        content = [
            {"type": "text",      "text": full_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        try:
            resp = self._client.chat.completions.create(
                model=self._model_id,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=256,
            )
            text = resp.choices[0].message.content
            if not text or not text.strip():
                return "API_ERROR: Empty response from model"
            return text.strip()
        except Exception as exc:
            return f"API_ERROR: {exc}"


# ═══════════════════════════════════════════════════════════════════════════════
#  ANSWER OVERLAY  (auto-dismissing top-right popup)
# ═══════════════════════════════════════════════════════════════════════════════

class AnswerOverlay(tk.Toplevel):
    AUTO_MS = 9_000
    WIDTH   = 380

    def __init__(self, master, raw: str):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.96)
        self.configure(bg="#0d0d0f")

        sw = self.winfo_screenwidth()
        self.geometry(f"{self.WIDTH}x10+{sw - self.WIDTH - 20}+20")

        self._answers = self._parse(raw)
        self._job     = None
        self._t0      = time.time()
        self._drag_x  = self._drag_y = 0

        self._build()
        self.update_idletasks()
        h = self.winfo_reqheight()
        self.geometry(f"{self.WIDTH}x{h}+{sw - self.WIDTH - 20}+20")
        self._job = self.after(self.AUTO_MS, self.close)
        self._tick()

    @staticmethod
    def _parse(raw: str) -> list[dict]:
        result: list[dict] = []
        counter = 1
        for line in (l.strip() for l in raw.splitlines() if l.strip()):
            line = re.sub(
                r'^(single|multiple|matching)\s*(choice)?\s*:?\s*', '',
                line, flags=re.I).strip()
            if not line:
                continue
            m = re.match(r'^([A-Z])-(\d+)$', line)
            if m:
                result.append({"n": m.group(1), "t": f"→ {m.group(2)}"}); counter += 1; continue
            m = re.match(r'^(\d+)[:.]\s*(.+)', line)
            if m:
                result.append({"n": m.group(1), "t": m.group(2).strip()}); counter += 1; continue
            result.append({"n": str(counter), "t": line}); counter += 1
        return result

    def _build(self):
        outer = tk.Frame(self, bg=NEON, padx=1, pady=1)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg="#0d0d0f")
        inner.pack(fill="both", expand=True)

        bar = tk.Frame(inner, bg="#0a1a0f", height=32)
        bar.pack(fill="x"); bar.pack_propagate(False)
        tk.Label(bar, text="⚡ AI ANSWERS", bg="#0a1a0f", fg=NEON,
                 font=("Consolas", 10, "bold"), padx=10).pack(side="left", pady=5)
        n = len(self._answers)
        tk.Label(bar, text=f"{n} answer{'s' if n != 1 else ''}",
                 bg="#0a1a0f", fg=DIM, font=("Consolas", 8)).pack(side="left")
        close_btn = tk.Label(bar, text=" ✕ ", bg="#0a1a0f", fg=RED,
                             font=("Consolas", 12, "bold"), cursor="hand2")
        close_btn.pack(side="right", padx=4)
        close_btn.bind("<Button-1>", lambda _: self.close())
        close_btn.bind("<Enter>",    lambda _: close_btn.configure(bg="#1a0505"))
        close_btn.bind("<Leave>",    lambda _: close_btn.configure(bg="#0a1a0f"))
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>",      self._drag_move)

        tk.Frame(inner, bg=NEON, height=1).pack(fill="x")

        card_area = tk.Frame(inner, bg="#0d0d0f")
        card_area.pack(fill="both", expand=True, padx=12, pady=8)
        for idx, ans in enumerate(self._answers):
            self._card(card_area, ans, idx)

        tk.Frame(inner, bg="#111", height=1).pack(fill="x")
        bar_bg = tk.Frame(inner, bg="#111", height=3)
        bar_bg.pack(fill="x"); bar_bg.pack_propagate(False)
        self._progress = tk.Frame(bar_bg, bg=NEON, height=3)
        self._progress.place(x=0, y=0, relwidth=1.0, height=3)

    def _card(self, parent: tk.Frame, ans: dict, idx: int):
        is_last = idx == len(self._answers) - 1
        card = tk.Frame(parent, bg="#111822",
                        highlightbackground="#1a3a2a", highlightthickness=1)
        card.pack(fill="x", pady=(0, 0 if is_last else 4))
        row = tk.Frame(card, bg="#111822")
        row.pack(fill="x", padx=8, pady=7)
        pill = tk.Frame(row, bg=NEON, width=22, height=22)
        pill.pack(side="left", padx=(0, 8)); pill.pack_propagate(False)
        tk.Label(pill, text=ans["n"], bg=NEON, fg="#000",
                 font=("Consolas", 8, "bold")).pack(expand=True)
        tk.Label(row, text=ans["t"], bg="#111822", fg=WHITE,
                 font=("Consolas", 11, "bold"),
                 wraplength=self.WIDTH - 80, justify="left", anchor="w",
                 ).pack(side="left", fill="x", expand=True)

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.winfo_x()
        self._drag_y = e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _tick(self):
        if not self.winfo_exists():
            return
        frac = max(0.0, 1.0 - (time.time() - self._t0) * 1000 / self.AUTO_MS)
        self._progress.place(x=0, y=0, relwidth=frac, height=3)
        self._progress.configure(
            bg=NEON if frac > 0.5 else (YELLOW if frac > 0.25 else RED))
        if frac > 0:
            self.after(50, self._tick)

    def close(self):
        if self._job:
            try: self.after_cancel(self._job)
            except Exception: pass
        try: self.destroy()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
#  REGION SELECTOR  (F8 drag overlay)
# ═══════════════════════════════════════════════════════════════════════════════

class RegionSelector(tk.Toplevel):
    def __init__(self, master, on_done):
        super().__init__(master)
        self._on_done  = on_done
        self._sx = self._sy = 0
        self._dragging = False
        self._rect = None

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.35)
        self.geometry(f"{sw}x{sh}+0+0")
        self.configure(bg="#001a00")
        self.config(cursor="crosshair")

        self._cv = tk.Canvas(self, bg="#001a00", highlightthickness=0,
                             width=sw, height=sh)
        self._cv.pack(fill="both", expand=True)
        self._hint = self._cv.create_text(
            sw // 2, 40,
            text="Drag to select region  •  ESC to cancel",
            fill=NEON, font=("Consolas", 13, "bold"))

        self._cv.bind("<ButtonPress-1>",   self._press)
        self._cv.bind("<B1-Motion>",        self._drag)
        self._cv.bind("<ButtonRelease-1>",  self._release)
        self.bind("<Escape>", lambda _: self._cancel())

    def _press(self, e):
        self._sx, self._sy = e.x, e.y
        self._dragging = True
        if self._rect:
            self._cv.delete(self._rect)
        self._rect = self._cv.create_rectangle(
            e.x, e.y, e.x, e.y,
            outline=NEON, width=2, fill="#003322", stipple="gray25")

    def _drag(self, e):
        if not self._dragging or not self._rect:
            return
        self._cv.coords(self._rect, self._sx, self._sy, e.x, e.y)
        cx = (self._sx + e.x) // 2
        cy = min(self._sy, e.y) - 22
        if cy < 16:
            cy = max(self._sy, e.y) + 22
        self._cv.coords(self._hint, cx, cy)

    def _release(self, e):
        if not self._dragging:
            return
        self._dragging = False
        x1, y1 = min(self._sx, e.x), min(self._sy, e.y)
        x2, y2 = max(self._sx, e.x), max(self._sy, e.y)
        w, h = x2 - x1, y2 - y1
        if w < 10 or h < 10:
            self._cancel(); return
        if self._rect:
            self._cv.itemconfig(self._rect, fill=NEON, stipple="")
        self.after(120, lambda: self._finish(x1, y1, w, h))

    def _finish(self, x, y, w, h):
        try: self.destroy()
        except Exception: pass
        self._on_done(x, y, w, h)

    def _cancel(self):
        try: self.destroy()
        except Exception: pass
        self._on_done(None, None, None, None)


# ═══════════════════════════════════════════════════════════════════════════════
#  LOG WIDGET
# ═══════════════════════════════════════════════════════════════════════════════

class LogWidget(tk.Frame):
    MAX = 200
    STYLES = {
        "ok":     ("#00c46e", "#000", NEON),
        "fail":   ("#c43030", "#fff", "#ff7070"),
        "answer": ("#1a5a35", NEON,   WHITE),
        "info":   ("#1a2a4a", BLUE,   BLUE),
        "warn":   ("#5a4800", YELLOW, YELLOW),
    }

    def __init__(self, master, **kw):
        super().__init__(master, bg=BG, **kw)
        self._rows: list = []
        cv = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(self, orient="vertical", command=cv.yview,
                          width=5, bg=BG2, troughcolor=BG)
        self._inner = tk.Frame(cv, bg=BG)
        self._win   = cv.create_window((0, 0), window=self._inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        cv.pack(side="left", fill="both", expand=True)
        self._cv = cv
        self._inner.bind("<Configure>",
                         lambda _: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>",
                lambda e: cv.itemconfig(self._win, width=e.width))
        cv.bind("<MouseWheel>",
                lambda e: cv.yview_scroll(-1 * (e.delta // 120), "units"))

    def _add(self, kind: str, icon: str, text: str):
        pb, pf, tf = self.STYLES.get(kind, self.STYLES["info"])
        row = tk.Frame(self._inner, bg=BG)
        row.pack(fill="x", padx=5, pady=1)
        pill = tk.Frame(row, bg=pb, width=18, height=15)
        pill.pack(side="left", padx=(0, 6), pady=1); pill.pack_propagate(False)
        tk.Label(pill, text=icon, bg=pb, fg=pf,
                 font=("Consolas", 7, "bold")).pack(expand=True)
        tk.Label(row, text=text, bg=BG, fg=tf,
                 font=("Consolas", 9), anchor="w", justify="left",
                 wraplength=340).pack(side="left", fill="x", expand=True, pady=1)
        self._rows.append(row)
        if len(self._rows) > self.MAX:
            try: self._rows.pop(0).destroy()
            except Exception: pass
        self._cv.update_idletasks()
        self._cv.yview_moveto(1.0)

    def divider(self):
        f = tk.Frame(self._inner, bg="#1a2f1a", height=1)
        f.pack(fill="x", padx=8, pady=3)
        self._rows.append(f)

    def clear(self):
        for w in self._rows:
            try: w.destroy()
            except Exception: pass
        self._rows.clear()

    def ok(self, t):     self._add("ok",     "✓", t)
    def fail(self, t):   self._add("fail",   "✗", t)
    def answer(self, t): self._add("answer", "→", t)
    def info(self, t):   self._add("info",   "i", t)
    def warn(self, t):   self._add("warn",   "!", t)


# ═══════════════════════════════════════════════════════════════════════════════
#  HOTKEY DIALOG
# ═══════════════════════════════════════════════════════════════════════════════

class HotkeyDialog(ctk.CTkToplevel):
    """
    Modal dialog for remapping hotkeys.
    Opens on top of parent (deferred grab_set avoids compositor race).
    Ctrl+V works via explicit <<Paste>> binding inside the grabbed window.
    """

    ACTIONS = [
        ("select_region", "Select Region", "F8",           YELLOW),
        ("solve",         "Solve (AI)",    "CTRL+SHIFT+S", NEON),
    ]

    def __init__(self, master, on_save):
        super().__init__(master)
        self._master  = master
        self._on_save = on_save
        self._entries: dict[str, ctk.CTkEntry] = {}

        self.title("Configure Hotkeys")
        self.geometry("400x290")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(80, self._grab_focus)
        self._build()

    def _grab_focus(self):
        try:
            self.lift()
            self.attributes("-topmost", True)
            self.grab_set()
            self.focus_force()
            first = next(iter(self._entries.values()))
            first.focus_set()
            first.icursor("end")
        except Exception:
            pass

    def _build(self):
        hdr = ctk.CTkFrame(self, fg_color=BG2, corner_radius=0)
        hdr.pack(fill="x")
        ctk.CTkLabel(hdr, text="⌨  Configure Hotkeys",
                     font=("Consolas", 13, "bold"), text_color=NEON,
                     ).pack(side="left", padx=16, pady=12)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=10)

        for key, label, default, color in self.ACTIONS:
            row = ctk.CTkFrame(body, fg_color=BG2, corner_radius=6)
            row.pack(fill="x", pady=5)
            ctk.CTkLabel(row, text=label, width=130,
                         font=("Consolas", 10, "bold"), text_color=color,
                         anchor="w").pack(side="left", padx=(12, 4), pady=10)
            e = ctk.CTkEntry(row, width=180, font=("Consolas", 10),
                             fg_color="#0d1a0d",
                             border_color=BORDER, border_width=1)
            e.insert(0, self._master._hotkeys.get(key, default))
            e.pack(side="left", padx=(0, 12), pady=8)
            e.bind("<<Paste>>", lambda ev, ew=e: self._paste_into(ev, ew))
            self._entries[key] = e

        ctk.CTkLabel(body, text="Examples:  f8   •   ctrl+shift+s   •   alt+z",
                     font=("Consolas", 8), text_color=DIM).pack(pady=(2, 0))

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=12)
        ctk.CTkButton(btn_row, text="✓  Save",
                      width=130, height=34,
                      font=("Consolas", 11, "bold"),
                      fg_color="#0d3320", hover_color="#1a5535",
                      border_color=NEON, border_width=1,
                      text_color=NEON,
                      command=self._save).pack(side="left", padx=8)
        ctk.CTkButton(btn_row, text="✕  Cancel",
                      width=130, height=34,
                      font=("Consolas", 11, "bold"),
                      fg_color="#1a0505", hover_color="#2a0808",
                      border_color="#553333", border_width=1,
                      text_color="#cc6666",
                      command=self._cancel).pack(side="left", padx=8)

    @staticmethod
    def _paste_into(_, entry: ctk.CTkEntry):
        try:
            clip = entry.clipboard_get()
            try:    entry.delete("sel.first", "sel.last")
            except Exception: pass
            entry.insert("insert", clip)
        except Exception:
            pass
        return "break"

    def _save(self):
        new_hk = {key: self._entries[key].get().strip()
                  for key, *_ in self.ACTIONS}
        self._on_save(new_hk)
        self._close()

    def _cancel(self):
        self._close()

    def _close(self):
        try:   self.grab_release()
        except Exception: pass
        try:   self.destroy()
        except Exception: pass


# ═══════════════════════════════════════════════════════════════════════════════
#  SPINNER
# ═══════════════════════════════════════════════════════════════════════════════

class Spinner:
    _FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: ctk.CTkLabel, prefix: str = "AI thinking"):
        self._lbl    = label
        self._prefix = prefix
        self._idx    = 0
        self._active = False
        self._job    = None

    def start(self):
        self._active = True
        self._tick()

    def stop(self):
        self._active = False
        if self._job:
            try: self._lbl.after_cancel(self._job)
            except Exception: pass

    def _tick(self):
        if not self._active:
            return
        self._lbl.configure(
            text=f"● {self._prefix} {self._FRAMES[self._idx % len(self._FRAMES)]}",
            text_color=NEON)
        self._idx += 1
        self._job  = self._lbl.after(80, self._tick)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AI Screen Assistant")
        self.geometry("520x760")
        self.minsize(480, 600)
        self.attributes("-topmost", True)

        self._settings = load_settings()
        self._hotkeys  = self._settings.get("hotkeys", DEFAULT_HOTKEYS.copy())

        # Apply saved scale before building UI
        self._scale = self._settings.get("scale", SCALE_DEFAULT)
        ctk.set_widget_scaling(self._scale)
        ctk.set_window_scaling(self._scale)

        self._hk_queue: queue.Queue        = queue.Queue()
        self.region: tuple | None          = None
        self._answer_overlay: AnswerOverlay | None = None
        self._ai: AIBackend | None         = None
        self._ai_cache_key: tuple          = ("", "")   # (api_key, model_id)
        self._spinner: Spinner | None      = None

        self.protocol("WM_DELETE_WINDOW", self._quit)
        self._build_ui()
        self._register_hotkeys()
        self.after(50, self._poll_hotkeys)
        self._check_tesseract()

        if self._settings.get("api_key"):
            self._key_entry.insert(0, self._settings["api_key"])

    # ── startup ───────────────────────────────────────────────────────────────

    def _check_tesseract(self):
        err = check_tesseract()
        if err:
            self._log("warn", "Tesseract not found — OCR will fail.")
            self._log("warn", "Install Tesseract or set TESSERACT_PATH env var.")
            self._set_status("Tesseract missing", RED)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ───────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="⚡ AI SCREEN ASSISTANT",
                     font=("Consolas", 22, "bold"), text_color=NEON,
                     ).pack(pady=(16, 2))

        self._sep()

        # ── Model selector ────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="MODEL",
                     font=("Consolas", 8, "bold"), text_color=DIM,
                     ).pack(anchor="w", padx=22)

        saved_id = self._settings.get("model_id", DEFAULT_MODEL_ID)
        # Guard: if saved id is no longer in MODELS list, fall back to default
        valid_ids = {m["id"] for m in MODELS}
        if saved_id not in valid_ids:
            saved_id = DEFAULT_MODEL_ID

        self._model_var = ctk.StringVar(
            value=next(m["label"] for m in MODELS if m["id"] == saved_id))

        model_labels = [m["label"] for m in MODELS]
        self._model_menu = ctk.CTkOptionMenu(
            self,
            variable=self._model_var,
            values=model_labels,
            command=self._on_model_change,
            width=460, height=34,
            font=("Consolas", 11),
            fg_color=BG2,
            button_color="#1a3a1a",
            button_hover_color="#2a5a2a",
            dropdown_fg_color=BG2,
            dropdown_hover_color="#1a3a1a",
            dropdown_text_color=WHITE,
        )
        self._model_menu.pack(pady=(2, 0), padx=20, fill="x")

        self._sep()

        # ── API key ───────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text="API KEY  (OpenRouter)",
                     font=("Consolas", 8, "bold"), text_color=DIM,
                     ).pack(anchor="w", padx=22)

        key_row = ctk.CTkFrame(self, fg_color="transparent")
        key_row.pack(fill="x", padx=20, pady=(2, 0))

        self._key_entry = ctk.CTkEntry(
            key_row, placeholder_text="sk-or-v1-...",
            show="*", font=("Consolas", 11), height=36,
            fg_color=BG2, border_color=BORDER, border_width=1)
        self._key_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        for seq in ("<Control-v>", "<Control-V>", "<<Paste>>"):
            self._key_entry.bind(seq, self._paste)
        self._key_entry.bind("<KeyRelease>", self._on_key_change)

        ctk.CTkButton(key_row, text="⎘ Paste",
                      width=82, height=36,
                      font=("Consolas", 10, "bold"),
                      fg_color="#0d2a1a", hover_color="#1a4a2a",
                      border_color=NEON, border_width=1,
                      text_color=NEON,
                      command=self._paste).pack(side="left")

        self._sep()

        # ── Action buttons + scale controls ───────────────────────────────────
        act = ctk.CTkFrame(self, fg_color="transparent")
        act.pack(fill="x", padx=20, pady=(0, 4))

        ctk.CTkButton(act, text="⌨  Hotkeys",
                      height=32, font=("Consolas", 10, "bold"),
                      fg_color="#0d1a2a", hover_color="#1a2f45",
                      border_color="#1a3a5a", border_width=1,
                      text_color=BLUE,
                      command=self._open_hotkey_dialog,
                      ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(act, text="⟳  Reset Region",
                      height=32, font=("Consolas", 10, "bold"),
                      fg_color="#0d1a0d", hover_color="#1a3a1a",
                      border_color="#1a5a3c", border_width=1,
                      text_color=NEON,
                      command=self._reset_region,
                      ).pack(side="left", padx=(0, 8))

        # Scale controls — pushed to the right
        scale_frame = ctk.CTkFrame(act, fg_color="transparent")
        scale_frame.pack(side="right")

        self._scale_lbl = ctk.CTkLabel(
            scale_frame,
            text=f"{int(self._scale * 100)}%",
            font=("Consolas", 9), text_color=DIM, width=38)
        self._scale_lbl.pack(side="left", padx=(0, 4))

        for symbol, delta in [("A−", -SCALE_STEP), ("A+", +SCALE_STEP)]:
            ctk.CTkButton(scale_frame, text=symbol,
                          width=36, height=32,
                          font=("Consolas", 10, "bold"),
                          fg_color=BG2, hover_color="#1a2a1a",
                          border_color=BORDER, border_width=1,
                          text_color=DIM,
                          command=lambda d=delta: self._change_scale(d),
                          ).pack(side="left", padx=2)

        # ── Hotkey reference card ─────────────────────────────────────────────
        self._sep()
        ref = ctk.CTkFrame(self, fg_color=BG2, corner_radius=8)
        ref.pack(padx=20, pady=(0, 4), fill="x")

        self._hk_pills: dict[str, ctk.CTkLabel] = {}
        for key, desc, color in [
            ("select_region", "select region", YELLOW),
            ("solve",         "solve",         NEON),
        ]:
            row = ctk.CTkFrame(ref, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=4)
            pill_text = self._hotkeys.get(key, DEFAULT_HOTKEYS[key]).upper()
            pill = ctk.CTkLabel(row, text=pill_text,
                                font=("Consolas", 8, "bold"),
                                text_color="#000", fg_color=color,
                                corner_radius=4, padx=6, pady=2)
            pill.pack(side="left", padx=(0, 8))
            ctk.CTkLabel(row, text=f"— {desc}",
                         font=("Consolas", 10), text_color=color,
                         anchor="w").pack(side="left")
            self._hk_pills[key] = pill

        # ── Status ────────────────────────────────────────────────────────────
        self._sep()
        self._status_lbl = ctk.CTkLabel(
            self, text="● Press F8 to select a region",
            font=("Consolas", 10, "bold"), text_color=YELLOW)
        self._status_lbl.pack(pady=(4, 2))
        self._spinner = Spinner(self._status_lbl)

        # ── Log ───────────────────────────────────────────────────────────────
        log_outer = tk.Frame(self, bg=BORDER, bd=1, relief="flat")
        log_outer.pack(padx=18, pady=4, fill="both", expand=True)

        log_hdr = tk.Frame(log_outer, bg="#0a1a0f", height=24)
        log_hdr.pack(fill="x"); log_hdr.pack_propagate(False)
        tk.Label(log_hdr, text="  ▸ LOG", bg="#0a1a0f", fg=NEON,
                 font=("Consolas", 9, "bold")).pack(side="left", padx=2)
        clr = tk.Label(log_hdr, text="clear  ", bg="#0a1a0f", fg=DIM,
                       font=("Consolas", 9), cursor="hand2")
        clr.pack(side="right")
        clr.bind("<Button-1>", lambda _: self.log.clear())
        clr.bind("<Enter>",    lambda _: clr.configure(fg=NEON))
        clr.bind("<Leave>",    lambda _: clr.configure(fg=DIM))

        tk.Frame(log_outer, bg="#1a3a1a", height=1).pack(fill="x")
        self.log = LogWidget(log_outer)
        self.log.pack(fill="both", expand=True)

    def _sep(self):
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=8)

    # ── scaling ───────────────────────────────────────────────────────────────

    def _change_scale(self, delta: float):
        new = round(self._scale + delta, 2)
        new = max(SCALE_MIN, min(SCALE_MAX, new))
        if new == self._scale:
            return
        self._scale = new
        ctk.set_widget_scaling(new)
        ctk.set_window_scaling(new)
        self._scale_lbl.configure(text=f"{int(new * 100)}%")
        self._settings["scale"] = new
        save_settings(self._settings)

    # ── model ────────────────────────────────────────────────────────────────

    def _on_model_change(self, label: str):
        model_id = next((m["id"] for m in MODELS if m["label"] == label), DEFAULT_MODEL_ID)
        self._settings["model_id"] = model_id
        save_settings(self._settings)
        self._ai = None   # invalidate cached backend

    def _get_model_id(self) -> str:
        label = self._model_var.get()
        return next((m["id"] for m in MODELS if m["label"] == label), DEFAULT_MODEL_ID)

    # ── hotkeys ───────────────────────────────────────────────────────────────

    def _register_hotkeys(self):
        keyboard.unhook_all()
        keyboard.add_hotkey(
            self._hotkeys.get("select_region", DEFAULT_HOTKEYS["select_region"]),
            lambda: self._hk_queue.put("select_region"))
        keyboard.add_hotkey(
            self._hotkeys.get("solve", DEFAULT_HOTKEYS["solve"]),
            lambda: self._hk_queue.put("solve"))

    def _poll_hotkeys(self):
        try:
            while True:
                action = self._hk_queue.get_nowait()
                if action == "select_region":
                    self._open_selector()
                elif action == "solve":
                    self._start_solve()
        except queue.Empty:
            pass
        self.after(50, self._poll_hotkeys)

    def _open_hotkey_dialog(self):
        HotkeyDialog(self, self._apply_hotkeys)

    def _apply_hotkeys(self, new_hk: dict):
        self._hotkeys = new_hk
        self._settings["hotkeys"] = new_hk
        save_settings(self._settings)
        self._register_hotkeys()
        for key, pill in self._hk_pills.items():
            pill.configure(text=new_hk.get(key, DEFAULT_HOTKEYS[key]).upper())
        self._log("ok", "Hotkeys updated")

    # ── region selection ──────────────────────────────────────────────────────

    def _open_selector(self):
        self.withdraw()
        self.after(80, lambda: RegionSelector(self, self._on_region_selected))

    def _on_region_selected(self, x, y, w, h):
        self.deiconify()
        if x is None:
            self._set_status("Selection cancelled", YELLOW)
            return
        self.region = (x, y, w, h)
        self._set_status(f"Region ready  {w}×{h}", NEON)
        self._log("ok", f"Region set  {w}×{h} px")

    def _reset_region(self):
        self.region = None
        self._set_status("Press F8 to select a region", YELLOW)
        self._log("info", "Region reset")

    # ── solve pipeline ────────────────────────────────────────────────────────

    def _start_solve(self):
        if not self.region:
            self._set_status("No region — press F8", RED); return
        key = self._key_entry.get().strip()
        if not key:
            self._set_status("API key is empty", RED); return
        model_id = self._get_model_id()
        threading.Thread(
            target=self._run_solve, args=(key, model_id), daemon=True).start()

    def _run_solve(self, api_key: str, model_id: str):
        model_label = next((m["label"] for m in MODELS if m["id"] == model_id), model_id)
        self._log_div()
        self._log("info", f"Model: {model_label}")
        self.after(0, self._spinner.start)
        try:
            img           = pyautogui.screenshot(region=self.region)
            rows, ocr_txt = run_ocr(img, self.region)
            if not rows:
                self._log("warn", "OCR returned nothing — check region")

            answer = self._call_ai(img, ocr_txt, api_key, model_id)
            if answer is None:
                return

            lines = self._parse_answer(answer)
            self._show_answer_overlay(answer)
            self._click_answers(lines, rows)
            self._set_status("Done!", NEON)

        except Exception as exc:
            self._log("fail", f"Unexpected error: {exc}")
            self._set_status("ERROR", RED)
        finally:
            self.after(0, self._spinner.stop)

    def _call_ai(self, img: Image.Image, ocr_txt: str,
                 api_key: str, model_id: str) -> str | None:
        cache_key = (api_key, model_id)
        if self._ai is None or self._ai_cache_key != cache_key:
            self._ai           = AIBackend(api_key, model_id)
            self._ai_cache_key = cache_key

        raw = self._ai.ask(SOLVE_PROMPT, img, ocr_txt)

        if raw.startswith("API_ERROR:"):
            err = raw[len("API_ERROR:"):]
            if   "Empty response" in err:                   msg, st = "Model returned empty response",  "Empty response"
            elif "402" in err or "credits" in err.lower(): msg, st = "No credits — top up OpenRouter",  "No credits"
            elif "401" in err or "auth"    in err.lower(): msg, st = "Invalid API key",                 "Invalid key"
            elif "429" in err or "rate"    in err.lower(): msg, st = "Rate limited — retry later",      "Rate limited"
            else:                                           msg, st = f"API error: {err[:60]}",          "API ERROR"
            self._log("warn", msg)
            self._set_status(st, RED)
            return None

        if not raw.strip():
            self._log("warn", "Empty response from model")
            self._set_status("Empty response", RED)
            return None

        return raw

    def _parse_answer(self, raw: str) -> list[str]:
        lines = []
        for line in (l.strip() for l in raw.splitlines() if l.strip()):
            if re.match(r'^(single|multiple|matching)\s*(choice)?\s*:?\s*$', line, re.I):
                continue
            clean = re.sub(r'^(single|multiple|matching)\s*(choice)?\s*:?\s*',
                           '', line, flags=re.I).strip()
            if not clean:
                continue
            lines.append(clean)
            disp = re.sub(r'^\d+[:.\s]+', '', clean).strip()
            self._log("answer", disp)
        return lines

    def _click_answers(self, lines: list[str], rows: dict):
        if not self.region or not rows:
            return
        rx, ry, _, rh = self.region
        clicked: set[int] = set()
        for raw in lines:
            if not raw:
                continue
            cx, cy, score = find_click(raw, rows, self.region)
            disp = re.sub(r'^\d+[:.\s]+', '', raw).strip()[:45]
            if cy is None or score < 0.50:
                self._log("fail", f'Not found: "{disp}"'); continue
            if any(abs(cy - py) < 30 for py in clicked):
                self._log("warn", f'Duplicate skip: "{disp}"'); continue
            cy = max(ry + 2, min(cy, ry + rh - 2))
            pyautogui.click(cx, cy)
            clicked.add(cy)
            self._log("ok", f'Clicked: "{disp}"')
            time.sleep(0.45)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _show_answer_overlay(self, raw: str):
        def _do():
            if self._answer_overlay:
                try: self._answer_overlay.close()
                except Exception: pass
            self._answer_overlay = AnswerOverlay(self, raw)
        self.after(0, _do)

    def _set_status(self, text: str, color: str = YELLOW):
        self.after(0, lambda: self._status_lbl.configure(
            text=f"● {text}", text_color=color))

    def _log(self, kind: str, text: str):
        fn = getattr(self.log, kind, self.log.info)
        self.after(0, lambda: fn(text))

    def _log_div(self):
        self.after(0, self.log.divider)

    def _paste(self, _e=None):
        try:
            clip = self.clipboard_get()
            self._key_entry.delete(0, "end")
            self._key_entry.insert(0, clip)
            self._on_key_change()
        except Exception:
            pass
        return "break"

    def _on_key_change(self, _e=None):
        self._settings["api_key"] = self._key_entry.get().strip()
        save_settings(self._settings)

    def _quit(self):
        keyboard.unhook_all()
        save_settings(self._settings)
        self.destroy()
        sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()
