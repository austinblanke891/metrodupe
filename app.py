# Metrodle Dupe — Blank SVG Edition (UI-polished, no Cairo)
# Functional game/calibration unchanged; layout + live suggestions + feedback added.

import base64
import csv
import datetime as dt
import io
import os
import random
import re
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import streamlit as st
from PIL import Image
import fitz  # PyMuPDF

# Optional (recommended) click helper
try:
    from streamlit_image_coordinates import streamlit_image_coordinates
except Exception:
    streamlit_image_coordinates = None


# -------------------- PATHS --------------------
BASE_DIR = os.path.dirname(__file__)
SVG_PATH = r"C:\Users\Austin\OneDrive - Blanke Advisors\Desktop\Metrodle Dupe\maps\tube_map_clean.svg"          # BLANK SVG
PDF_PATH = r"C:\Users\Austin\OneDrive - Blanke Advisors\Desktop\Metrodle Dupe\maps\large-print-tube-map.pdf"   # LABELED PDF
DB_PATH  = os.path.join(BASE_DIR, "stations_db.csv")  # created automatically

# -------------------- TUNING --------------------
VIEW_W, VIEW_H = 980, 620     # viewport in the app
ZOOM           = 3.0          # how much the map is zoomed into the station
RING_PX        = 28           # ring radius in pixels (min clamp)
RING_STROKE    = 6
MAX_GUESSES    = 6

# Calibration image sizing (keeps payload < 200MB)
CAL_DPI        = 120          # raster DPI for PDF
CAL_MAX_W      = 1400         # max raster width (server side)
CAL_DISPLAY_W  = 1000         # width shown in browser during calibration


# -------------------- DATA --------------------
@dataclass
class Station:
    name: str
    fx: float
    fy: float
    lines: List[str]
    @property
    def key(self) -> str:
        return norm(self.name)

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def clean_display(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[’']", "", s)
    s = s.replace("&", "and")
    s = re.sub(r"\s+", " ", s).strip()
    return s


ALIASES = {
    "towerhamlets": "Tower Hill",
    "stpauls": "St Paul’s",
    "st pauls": "St Paul’s",
    "kings cross": "King’s Cross St. Pancras",
    "kings cross st pancras": "King’s Cross St. Pancras",
    "tottenham crt rd": "Tottenham Court Road",
    "tottenham court rd": "Tottenham Court Road",
}

LINES_CATALOG = [
    # Underground
    "Bakerloo", "Central", "Circle", "District", "Hammersmith & City",
    "Jubilee", "Metropolitan", "Northern", "Piccadilly", "Victoria", "Waterloo & City",
    # TfL modes
    "Elizabeth line", "Overground", "DLR", "Thameslink", "Tram", "National Rail",
]
def normalize_lines(lines: List[str]) -> List[str]:
    return sorted(set([(l or "").lower().strip() for l in lines if l]))


# -------------------- STORAGE --------------------
def ensure_db():
    if not os.path.exists(DB_PATH):
        with open(DB_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["name", "fx", "fy", "lines"])

def load_db() -> Tuple[List[Station], Dict[str, Station], List[str]]:
    ensure_db()
    stations: List[Station] = []
    with open(DB_PATH, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                name = clean_display(r["name"])
                fx = float(r["fx"]); fy = float(r["fy"])  # type: ignore
                lines = normalize_lines(r.get("lines", "").split(";"))
                if 0 <= fx <= 1 and 0 <= fy <= 1 and name:
                    stations.append(Station(name, fx, fy, lines))
            except Exception:
                continue
    by_key = {s.key: s for s in stations}
    return stations, by_key, sorted([s.name for s in stations])

def upsert_station(name: str, fx: float, fy: float, lines: List[str]):
    ensure_db()
    name = clean_display(name)
    key = norm(name)
    rows, found = [], False
    with open(DB_PATH, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            if norm(r["name"]) == key:
                r = {"name": name, "fx": f"{fx:.6f}", "fy": f"{fy:.6f}", "lines": ";".join(normalize_lines(lines))}
                found = True
            rows.append(r)
    if not found:
        rows.append({"name": name, "fx": f"{fx:.6f}", "fy": f"{fy:.6f}", "lines": ";".join(normalize_lines(lines))})
    with open(DB_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "fx", "fy", "lines"])
        w.writeheader(); [w.writerow(r) for r in rows]


# -------------------- SUGGEST/RESOLVE --------------------
def alias_name(q: str) -> str:
    return ALIASES.get(norm(q), q)

def resolve_guess(q: str, by_key: Dict[str, Station]) -> Optional[Station]:
    q = alias_name(q)
    nq = norm(q)
    if not nq: return None
    if nq in by_key: return by_key[nq]
    for s in by_key.values():
        if norm(s.name) == nq or norm(clean_display(s.name)) == nq:
            return s
    return None

def same_line(a: Station, b: Station) -> bool:
    return bool(set(a.lines) & set(b.lines))

def overlap_lines(a: Station, b: Station) -> List[str]:
    return sorted(list(set(a.lines) & set(b.lines)))

def prefix_suggestions(q: str, names: List[str], limit: int = 5) -> List[str]:
    """First `limit` station names that START WITH the typed text (case-insensitive)."""
    q = (q or "").strip().lower()
    if not q:
        return []
    matches = [n for n in names if n.lower().startswith(q)]
    return sorted(matches)[:limit]


# -------------------- ASSETS --------------------
@st.cache_resource(show_spinner=False)
def load_svg_data(svg_path: str) -> Tuple[str, float, float]:
    """Return (data_uri, baseW, baseH) for SVG; infer size from viewBox/width/height."""
    if not os.path.exists(svg_path):
        raise FileNotFoundError(f"SVG not found: {svg_path}")
    raw = open(svg_path, "rb").read()
    txt = raw.decode("utf-8", errors="ignore")
    m = re.search(r'viewBox="([\d.\s\-]+)"', txt)
    if m:
        _, _, w_str, h_str = m.group(1).split()
        base_w = float(w_str); base_h = float(h_str)
    else:
        def f(v): return float(re.sub(r"[^0-9.]", "", v)) if v else 3200.0
        w_attr = re.search(r'width="([^"]+)"', txt)
        h_attr = re.search(r'height="([^"]+)"', txt)
        base_w = f(w_attr.group(1) if w_attr else None)
        base_h = f(h_attr.group(1) if h_attr else None)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}", base_w, base_h

@st.cache_resource(show_spinner=False)
def render_pdf_page_to_png(pdf_path: str, dpi: int = CAL_DPI, max_width: int = CAL_MAX_W) -> Tuple[Image.Image, int, int]:
    """Rasterize page 0 of labeled PDF, then downscale to keep payload small."""
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    doc = fitz.open(pdf_path)
    page = doc[0]
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    if img.width > max_width:
        scale = max_width / float(img.width)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
    return img, img.width, img.height


# -------------------- GEOMETRY / HTML --------------------
def css_transform(baseW: float, baseH: float, fx: float, fy: float, zoom: float) -> Tuple[float, float]:
    cx, cy = fx * baseW, fy * baseH
    tx = VIEW_W / 2 - cx * zoom
    ty = VIEW_H / 2 - cy * zoom
    return tx, ty

def make_map_html(svg_uri: str, baseW: float, baseH: float, fx: float, fy: float,
                  zoom: float, colorize: bool, ring_color: str) -> str:
    tx, ty = css_transform(baseW, baseH, fx, fy, zoom)
    filt = "grayscale(0)" if colorize else "grayscale(1) brightness(1.02)"
    r_px = max(RING_PX, 0.010 * min(baseW, baseH) * zoom)
    return f"""
    <div style="position:relative;width:{VIEW_W}px;height:{VIEW_H}px;overflow:hidden;border-radius:14px;background:#f6f7f8;margin:0 auto;">
      <img src="{svg_uri}"
           style="position:absolute;top:0;left:0;width:{baseW}px;height:{baseH}px;
                  transform: translate({tx}px,{ty}px) scale({zoom}); transform-origin: top left; filter:{filt};">
      <div style="position:absolute;left:{VIEW_W/2 - r_px}px;top:{VIEW_H/2 - r_px}px;
                  width:{2*r_px}px;height:{2*r_px}px;border:{RING_STROKE}px solid {ring_color};
                  border-radius:50%;pointer-events:none;box-shadow:0 0 0 1px rgba(0,0,0,0.45) inset;"></div>
    </div>
    """


# -------------------- STREAMLIT APP --------------------
st.set_page_config(page_title="Metrodle — Blank SVG", page_icon="🗺️", layout="wide")
st.title("Metrodle — Blank SVG Edition")

with st.expander("Diagnostics", expanded=False):
    st.write("Python executable:", sys.executable)
    st.write("SVG exists:", os.path.exists(SVG_PATH))
    st.write("PDF exists:", os.path.exists(PDF_PATH))
    st.write("DB path:", DB_PATH)

# Load assets (no Cairo!)
SVG_URI, SVG_W, SVG_H = load_svg_data(SVG_PATH)

# session state
if "phase" not in st.session_state:
    st.session_state.phase="start"
    st.session_state.mode="daily"
    st.session_state.answer=None
    st.session_state.remaining=MAX_GUESSES
    st.session_state.history=[]
    st.session_state.won=False
    st.session_state.calib=False
if "guess_text" not in st.session_state:
    st.session_state["guess_text"] = ""   # filter box contents
if "feedback" not in st.session_state:
    st.session_state["feedback"] = ""     # feedback message for wrong guesses

c1,c2,c3 = st.columns([1,1,1])
with c1: st.radio("Mode",["daily","practice"],key="mode",horizontal=True)
with c2:
    if st.button("Open Calibration Mode"): st.session_state.calib=True
with c3:
    if st.session_state.calib and st.button("Back to Game"): st.session_state.calib=False

STATIONS, BY_KEY, NAMES = load_db()


# -------- Calibration (unchanged except for payload sizing) --------
if st.session_state.calib:
    st.subheader("Calibration")
    if streamlit_image_coordinates is None:
        st.warning(f'Install click helper:\n  "{sys.executable}" -m pip install streamlit-image-coordinates')
        st.stop()

    pdf_img, pdfW, pdfH = render_pdf_page_to_png(PDF_PATH, dpi=CAL_DPI, max_width=CAL_MAX_W)
    res = streamlit_image_coordinates(pdf_img, width=CAL_DISPLAY_W, key="calib_click")
    name = st.text_input("Station name")
    lines = st.multiselect("Lines (select all that apply)", options=LINES_CATALOG)

    fx = fy = None
    if res:
        disp_w = res.get("display_width", CAL_DISPLAY_W)
        scale = pdfW / float(disp_w)
        px = float(res["x"]) * scale
        py = float(res["y"]) * scale
        fx = px / pdfW
        fy = py / pdfH

        # Live preview (blank SVG) — centered
        html = make_map_html(SVG_URI, SVG_W, SVG_H, fx, fy, ZOOM, colorize=True, ring_color="#22c55e")
        _a, m, _b = st.columns([1,2,1])
        with m:
            st.components.v1.html(html, height=VIEW_H, scrolling=False)

    if st.button("Save station", type="primary", disabled=not(res and name and lines)):
        upsert_station(name, fx, fy, lines)
        st.success(f"Saved: {name}")
    st.stop()


# -------- Game helpers --------
def start_round() -> bool:
    if not STATIONS:
        st.warning("No stations calibrated yet — open Calibration Mode and add some.")
        return False
    # Reset round state
    st.session_state.phase="play"
    st.session_state.history=[]
    st.session_state.remaining=MAX_GUESSES
    st.session_state.won=False
    st.session_state["feedback"] = ""
    # Clear filter BEFORE the input is rendered in the next run
    if "guess_text" in st.session_state:
        del st.session_state["guess_text"]
    rng = random.Random(20250501 + dt.date.today().toordinal()) if st.session_state.mode=="daily" else random.Random()
    choice_name = rng.choice(NAMES)
    st.session_state.answer = BY_KEY[norm(choice_name)]
    return True

if st.session_state.phase=="start":
    _l, c, _r = st.columns([1,1,1])
    with c:
        if st.button("Start Game", type="primary", use_container_width=True):
            if start_round(): st.rerun()


# -------- Game play / end screens --------
if st.session_state.phase in ("play", "end") and STATIONS:
    answer: Station = st.session_state.answer or STATIONS[0]
    colorize=False
    if st.session_state.history:
        last = resolve_guess(st.session_state.history[-1], BY_KEY)
        if last and same_line(last, answer): colorize=True

    ring = "#22c55e" if (st.session_state.phase=="end" and st.session_state.won) else ("#eab308" if colorize else "#22c55e")

    # Center the map
    _L, mid, _R = st.columns([1,2,1])
    with mid:
        html = make_map_html(SVG_URI, SVG_W, SVG_H, answer.fx, answer.fy, ZOOM, colorize=colorize, ring_color=ring)
        st.components.v1.html(html, height=VIEW_H, scrolling=False)

        if st.session_state.phase == "play":
            # ---------- Styles ----------
            st.markdown(
                """
                <style>
                  .sugg-list {max-width: 540px; margin: 10px auto 6px auto;}
                  .sugg-item button {
                      width: 100%;
                      text-align: center;
                      padding: 8px 10px;
                      border-radius: 10px;
                      border: 1px solid rgba(255,255,255,0.12);
                      margin-bottom: 6px;
                  }
                  .sugg-item button:hover {background:#f6f7f8; color:#111}
                  .stTextInput>div>div>input {text-align:center}
                </style>
                """,
                unsafe_allow_html=True,
            )

            # Place suggestions ABOVE the input but compute them AFTER the input
            suggestions_box = st.container()

            # Make sure filter key exists each run (it was deleted on new round)
            if "guess_text" not in st.session_state:
                st.session_state["guess_text"] = ""

            # Text input acts only as a filter; user cannot submit from it
            q = st.text_input(
                "Type to search stations",
                key="guess_text",
                placeholder="Start typing…",
                label_visibility="collapsed"
            )

            # Now render suggestions (above the input) using a placeholder
            with suggestions_box:
                sugg = prefix_suggestions(q, NAMES, limit=5)
                if sugg:
                    st.markdown('<div class="sugg-list">', unsafe_allow_html=True)
                    for s in sugg:
                        if st.button(s, key=f"sugg_{s}", use_container_width=True):
                            st.session_state.history.append(s)
                            st.session_state.remaining -= 1
                            picked = resolve_guess(s, BY_KEY)
                            if picked and picked.key == answer.key:
                                st.session_state.won = True
                                st.session_state.phase = "end"
                                st.session_state["feedback"] = ""
                            else:
                                if picked and same_line(picked, answer):
                                    lines = ", ".join(overlap_lines(picked, answer)) or "right line"
                                    st.session_state["feedback"] = f"❌ Wrong station, but correct line ({lines})."
                                else:
                                    st.session_state["feedback"] = "❌ Wrong station."
                                if st.session_state.remaining <= 0:
                                    st.session_state.won = False
                                    st.session_state.phase = "end"
                            st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

            # Show feedback while still playing
            if st.session_state.get("feedback"):
                st.info(st.session_state["feedback"])

        # History / status
        if st.session_state.history:
            st.markdown("**Your guesses:** " + ", ".join(st.session_state.history))
        st.caption(f"Guesses left: {st.session_state.remaining}")

    # End-screen messaging (no input rendered here)
    if st.session_state.phase == "end":
        # make the play-again button big & centered
        st.markdown(
            """
            <style>
              .play-again .stButton>button {
                  font-size: 1.05rem;
                  padding: 12px 22px;
                  border-radius: 10px;
              }
            </style>
            """,
            unsafe_allow_html=True,
        )
        _l, c, _r = st.columns([1,1,1])
        with c:
            if st.session_state.won:
                st.success("Nice! You got it.")
            else:
                st.error(f"Out of guesses. The station was **{answer.name}**.")
            with st.container():
                st.markdown('<div class="play-again">', unsafe_allow_html=True)
                if st.button("Play again", type="primary", use_container_width=True):
                    if start_round(): st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
