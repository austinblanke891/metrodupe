# Tube Guessr — vector-cropped (crisp) map rendering
# - SVG is cropped/rasterized at VIEW_W×VIEW_H per round using CairoSVG
# - Grayscale is baked server-side (no blur-inducing browser filters)
# - Guess bar sits right under the map with minimal spacing

import base64
import csv
import datetime as dt
import io
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
import cairosvg
import streamlit as st

# -------------------- PATHS --------------------
BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "maps"
SVG_PATH = ASSETS_DIR / "tube_map_clean.svg"     # Original vector SVG
DB_PATH = BASE_DIR / "stations_db.csv"           # name,fx,fy,lines

# -------------------- TUNING --------------------
VIEW_W, VIEW_H = 980, 620
ZOOM        = 3.0
RING_PX     = 28
RING_STROKE = 6
MAX_GUESSES = 6

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

def normalize_lines(lines: List[str]) -> List[str]:
    return sorted(set([(l or "").lower().strip() for l in lines if l]))

# -------------------- STORAGE --------------------
def ensure_db():
    if not DB_PATH.exists():
        with open(DB_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["name", "fx", "fy", "lines"])

@st.cache_resource(show_spinner=False)
def load_db() -> Tuple[List[Station], Dict[str, Station], List[str]]:
    ensure_db()
    stations: List[Station] = []
    with open(DB_PATH, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                name = clean_display(r["name"])
                fx = float(r["fx"]); fy = float(r["fy"])
                lines = normalize_lines((r.get("lines") or "").split(";"))
                if 0 <= fx <= 1 and 0 <= fy <= 1 and name:
                    stations.append(Station(name, fx, fy, lines))
            except Exception:
                continue
    by_key = {s.key: s for s in stations}
    return stations, by_key, sorted([s.name for s in stations])

# -------------------- SUGGEST / RESOLVE --------------------
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
    q = (q or "").strip().lower()
    if not q:
        return []
    matches = [n for n in names if n.lower().startswith(q)]
    return sorted(matches)[:limit]

# -------------------- SVG / VECTOR RENDER --------------------
@st.cache_resource(show_spinner=False)
def load_svg_text(svg_path: Path) -> str:
    return svg_path.read_text("utf-8", errors="ignore")

def _parse_svg_size(svg_text: str) -> Tuple[float, float]:
    """Return (base_w, base_h) from viewBox or width/height."""
    m = re.search(r'viewBox\s*=\s*"([\d.\s\-]+)"', svg_text)
    if m:
        _, _, w, h = (float(x) for x in m.group(1).split())
        return w, h
    def _f(rx, default):
        m = re.search(rx, svg_text)
        if not m: return default
        v = re.sub(r"[^0-9.]", "", m.group(1))
        try: return float(v)
        except: return default
    return _f(r'width="([^"]+)"', 3200.0), _f(r'height="([^"]+)"', 2200.0)

def _crop_params(svg_text: str, fx_center: float, fy_center: float,
                 view_w: int, view_h: int, zoom: float) -> Tuple[float, float, float, float, float, float]:
    """Return base_w, base_h, x0, y0, crop_w, crop_h (all in SVG units)."""
    base_w, base_h = _parse_svg_size(svg_text)
    crop_w = view_w / zoom
    crop_h = view_h / zoom
    cx = fx_center * base_w
    cy = fy_center * base_h
    # Clamp within bounds
    x0 = max(0.0, min(base_w - crop_w, cx - crop_w / 2))
    y0 = max(0.0, min(base_h - crop_h, cy - crop_h / 2))
    return base_w, base_h, x0, y0, crop_w, crop_h

@st.cache_data(show_spinner=False)
def render_crop_data_uri(svg_text: str, fx_center: float, fy_center: float,
                         view_w: int, view_h: int, zoom: float,
                         grayscale: bool) -> str:
    """Crop original SVG at vector-level and rasterize directly to view_w×view_h."""
    base_w, base_h, x0, y0, crop_w, crop_h = _crop_params(svg_text, fx_center, fy_center, view_w, view_h, zoom)

    # Rewrite viewBox & explicit width/height for the crop
    out = svg_text
    if re.search(r'viewBox\s*=\s*"[^"]+"', out):
        out = re.sub(r'viewBox\s*=\s*"[^"]+"',
                     f'viewBox="{x0} {y0} {crop_w} {crop_h}"',
                     out, count=1)
    else:
        out = out.replace("<svg", f'<svg viewBox="{x0} {y0} {crop_w} {crop_h}"', 1)

    if re.search(r'width="[^"]+"', out):
        out = re.sub(r'width="[^"]+"',  f'width="{view_w}px"', out, count=1)
    else:
        out = out.replace("<svg", f'<svg width="{view_w}px"', 1)

    if re.search(r'height="[^"]+"', out):
        out = re.sub(r'height="[^"]+"', f'height="{view_h}px"', out, count=1)
    else:
        out = out.replace("<svg", f'<svg height="{view_h}px"', 1)

    # Vector -> PNG at final pixels
    png_bytes = cairosvg.svg2png(bytestring=out.encode("utf-8"),
                                 output_width=view_w,
                                 output_height=view_h)

    # Optional: bake grayscale (keeps sharpness)
    if grayscale:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        g = im.convert("L")
        im = Image.merge("RGBA", (g, g, g, im.split()[-1]))  # keep alpha
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        png_bytes = buf.getvalue()

    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"

def project_to_screen(svg_text: str, fx_center: float, fy_center: float,
                      fx_target: float, fy_target: float,
                      view_w: int, view_h: int, zoom: float) -> Tuple[float, float]:
    """Given center and a target (fx,fy), return on-screen px (sx,sy)."""
    base_w, base_h, x0, y0, crop_w, crop_h = _crop_params(svg_text, fx_center, fy_center, view_w, view_h, zoom)
    x = ((fx_target * base_w) - x0) / crop_w * view_w
    y = ((fy_target * base_h) - y0) / crop_h * view_h
    return x, y

def make_map_html_vector_crop(svg_text: str,
                              fx_center: float, fy_center: float,
                              zoom: float, colorize: bool, ring_color: str,
                              overlays: Optional[List[Tuple[float, float, str, float]]] = None) -> str:
    """Return HTML with the cropped PNG and an overlay SVG for ring/markers."""
    uri = render_crop_data_uri(svg_text, fx_center, fy_center, VIEW_W, VIEW_H, zoom, grayscale=(not colorize))
    r_px = max(RING_PX, 0.010 * min(VIEW_W, VIEW_H))

    overlay_svg = ""
    if overlays:
        parts = []
        for (sx, sy, color, rr) in overlays:
            parts.append(
                f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{rr:.1f}" '
                f'fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2"/>'
            )
        overlay_svg = "\n".join(parts)

    return f"""
    <div class="map-wrap" style="width:min(100%, {VIEW_W}px); margin:0 auto 6px auto; position:relative;">
      <img src="{uri}" width="{VIEW_W}" height="{VIEW_H}"
           style="display:block;border-radius:14px;" alt="map crop"/>
      <svg width="{VIEW_W}" height="{VIEW_H}"
           style="position:absolute;left:0;top:0;pointer-events:none;">
        <circle cx="{VIEW_W/2}" cy="{VIEW_H/2}" r="{r_px}"
                stroke="{ring_color}" stroke-width="{RING_STROKE}" fill="none"
                style="filter: drop-shadow(0 0 0 rgba(0,0,0,0.45));"/>
        {overlay_svg}
      </svg>
    </div>
    """

# -------------------- GAME HELPERS --------------------
def start_round(stations, by_key, names):
    if not stations:
        st.warning("No stations found in stations_db.csv.")
        return False
    st.session_state.phase = "play"
    st.session_state.history = []
    st.session_state.remaining = MAX_GUESSES
    st.session_state.won = False
    st.session_state["feedback"] = ""
    rng = random.Random(20250501 + dt.date.today().toordinal()) if st.session_state.mode == "daily" else random.Random()
    choice_name = rng.choice(names)
    st.session_state.answer = by_key[norm(choice_name)]
    return True

# -------------------- STREAMLIT APP --------------------
st.set_page_config(page_title="Tube Guessr", page_icon=None, layout="wide")

# Global CSS: tighter spacing; guess bar sits right under the map
st.markdown(
    """
    <style>
      .block-container { max-width: 1100px; padding-top: 1.4rem; padding-bottom: 1rem; }
      .block-container h1:first-of-type { margin: 0 0 .6rem 0; }
      .map-wrap { margin: 0 auto 6px auto !important; }
      .stTextInput { margin-top: 6px !important; margin-bottom: 6px !important; }
      .stTextInput>div>div>input {
        text-align: center; height: 44px; line-height: 44px; font-size: 1rem;
      }
      .sugg-list .stButton>button {
        min-height: 42px; font-size: 1rem; border-radius: 12px; margin: 8px 0 0 0;
      }
      .post-input { margin-top: 6px; }
      .play-center { display:flex; justify-content:center; }
      .play-center .stButton>button {
        min-width: 220px; border-radius: 9999px; padding: 10px 18px; font-size: 1rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# Session state
if "phase" not in st.session_state:
    st.session_state.phase = "welcome"
    st.session_state.mode = "daily"
    st.session_state.answer = None
    st.session_state.remaining = MAX_GUESSES
    st.session_state.history = []
    st.session_state.won = False
if "feedback" not in st.session_state:
    st.session_state["feedback"] = ""

# Load data/assets
SVG_TEXT = load_svg_text(SVG_PATH)
STATIONS, BY_KEY, NAMES = load_db()

# Helpers
def render_mode_picker(title_on_top=False):
    if title_on_top:
        st.markdown("### Mode")
    choice = st.radio(
        label="Mode",
        options=["daily", "practice"],
        index=(0 if st.session_state.mode == "daily" else 1),
        horizontal=True,
        label_visibility="collapsed",
    )
    st.session_state.mode = choice

def centered_play(label):
    st.markdown('<div class="play-center">', unsafe_allow_html=True)
    clicked = st.button(label, type="primary")
    st.markdown('</div>', unsafe_allow_html=True)
    return clicked

# -------------------- WELCOME --------------------
if st.session_state.phase == "welcome":
    st.markdown("# Tube Guessr")
    st.markdown(
        """
        Guess the London Underground station from a zoomed-in crop of the Tube map.

        **How to play**
        - Start typing a station name in the search box on the game screen, then press Enter.
        - A list of auto-fill suggestions will appear — click one to submit your guess.
        - If your guess is wrong but on the correct line, we’ll tell you (map tint turns amber).
        - You have 6 guesses.
        """
    )
    st.divider()
    if centered_play("Play"):
        st.session_state.phase = "start"
        st.rerun()

# -------------------- START --------------------
elif st.session_state.phase == "start":
    st.markdown("# Tube Guessr")
    render_mode_picker(title_on_top=True)
    st.write("")
    if centered_play("Start Game"):
        if start_round(STATIONS, BY_KEY, NAMES): st.rerun()

# -------------------- PLAY / END --------------------
elif st.session_state.phase in ("play", "end"):
    st.markdown("# Tube Guessr")
    render_mode_picker(title_on_top=True)

    answer: Station = st.session_state.answer or STATIONS[0]
    colorize = False
    if st.session_state.history:
        last = resolve_guess(st.session_state.history[-1], BY_KEY)
        if last and same_line(last, answer):
            colorize = True

    ring = "#22c55e" if (st.session_state.phase == "end" and st.session_state.won) else ("#eab308" if colorize else "#22c55e")

    # Build overlays for previous guesses (project onto current crop)
    overlays: List[Tuple[float, float, str, float]] = []
    for gname in st.session_state.history:
        st_obj = resolve_guess(gname, BY_KEY)
        if not st_obj or st_obj.key == answer.key:
            continue
        sx, sy = project_to_screen(SVG_TEXT, answer.fx, answer.fy, st_obj.fx, st_obj.fy, VIEW_W, VIEW_H, ZOOM)
        if 0 <= sx <= VIEW_W and 0 <= sy <= VIEW_H:
            color = "#f59e0b" if same_line(st_obj, answer) else "#ef4444"
            overlays.append((sx, sy, color, 30.0))

    # Center column: map + input + feedback
    _L, mid, _R = st.columns([1, 2, 1])
    with mid:
        html = make_map_html_vector_crop(
            SVG_TEXT, answer.fx, answer.fy, ZOOM, colorize, ring, overlays
        )
        st.markdown(html, unsafe_allow_html=True)

        if st.session_state.phase == "play":
            q_now = st.text_input(
                "Type to search stations",
                key="live_guess_box",
                placeholder="Start typing… then press Enter",
                label_visibility="collapsed",
            )
            sugg = prefix_suggestions(q_now or "", NAMES, limit=5)
            if sugg:
                st.markdown('<div class="sugg-list">', unsafe_allow_html=True)
                for s in sugg:
                    if st.button(s, key=f"sugg_{s}", use_container_width=True):
                        st.session_state.history.append(s)
                        st.session_state.remaining -= 1
                        chosen = resolve_guess(s, BY_KEY)
                        if chosen and chosen.key == answer.key:
                            st.session_state.won = True
                            st.session_state.phase = "end"
                            st.session_state["feedback"] = ""
                        else:
                            if chosen and same_line(chosen, answer):
                                lines = ", ".join(overlap_lines(chosen, answer)) or "right line"
                                st.session_state["feedback"] = f"Wrong station, but correct line ({lines})."
                            else:
                                st.session_state["feedback"] = "Wrong station."
                            if st.session_state.remaining <= 0:
                                st.session_state.won = False
                                st.session_state.phase = "end"
                        st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)

        if st.session_state.get("feedback"):
            st.info(st.session_state["feedback"])
        if st.session_state.history:
            st.markdown('<div class="post-input">**Your guesses:** ' + ", ".join(st.session_state.history) + "</div>", unsafe_allow_html=True)
        st.caption(f"Guesses left: {st.session_state.remaining}")

    if st.session_state.phase == "end":
        _l, c, _r = st.columns([1, 1, 1])
        with c:
            if st.session_state.won:
                st.success("Correct!")
            else:
                st.error(f"Out of guesses. The station was **{answer.name}**.")
            if centered_play("Play again"):
                if start_round(STATIONS, BY_KEY, NAMES): st.rerun()
