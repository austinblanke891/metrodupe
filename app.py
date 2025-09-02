# Tube Guessr — vector crop when possible, CSS crop fallback when CairoSVG isn't available.
# - If CairoSVG loads: we render a vector-accurate crop to PNG server-side (crisp, robust grayscale).
# - If CairoSVG is not available: we fall back to client-side SVG + CSS transform cropping (still crisp).
# - Guess bar is directly under the map with minimal spacing.

import base64
import csv
import datetime as dt
import io
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st
from PIL import Image

# Try CairoSVG; if it fails (system Cairo missing), we will use CSS-crop fallback.
CAIRO_OK = False
CAIRO_IMPORT_ERROR = None
try:
    import cairosvg  # type: ignore
    CAIRO_OK = True
except Exception as _e:
    CAIRO_OK = False
    CAIRO_IMPORT_ERROR = repr(_e)

# -------------------- PATHS --------------------
BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "maps"
SVG_PATH = ASSETS_DIR / "tube_map_clean.svg"     # Original SVG
DB_PATH  = BASE_DIR / "stations_db.csv"          # name,fx,fy,lines

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

# -------------------- ASSET LOADERS --------------------
@st.cache_resource(show_spinner=False)
def load_svg_text(svg_path: Path) -> str:
    return svg_path.read_text("utf-8", errors="ignore")

@st.cache_resource(show_spinner=False)
def load_svg_uri_and_size(svg_path: Path) -> Tuple[str, float, float]:
    raw = svg_path.read_bytes()
    txt = raw.decode("utf-8", errors="ignore")
    m = re.search(r'viewBox\s*=\s*"([\d.\s\-]+)"', txt)
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

# -------------------- GEOMETRY HELPERS --------------------
def _parse_svg_size(svg_text: str) -> Tuple[float, float]:
    m = re.search(r'viewBox\s*=\s*"([\d.\s\-]+)"', svg_text)
    if m:
        _, _, w, h = (float(x) for x in m.group(1).split())
        return w, h
    # fallback
    def _f(rx, default):
        m = re.search(rx, svg_text)
        if not m: return default
        v = re.sub(r"[^0-9.]", "", m.group(1))
        try: return float(v)
        except: return default
    return _f(r'width="([^"]+)"', 3200.0), _f(r'height="([^"]+)"', 2200.0)

def css_transform(baseW: float, baseH: float, fx_center: float, fy_center: float, zoom: float) -> Tuple[float, float]:
    cx, cy = fx_center * baseW, fy_center * baseH
    tx = VIEW_W / 2 - cx * zoom
    ty = VIEW_H / 2 - cy * zoom
    return tx, ty

def project_to_screen_css(baseW: float, baseH: float, fx_c: float, fy_c: float,
                          fx_t: float, fy_t: float, zoom: float) -> Tuple[float, float]:
    tx, ty = css_transform(baseW, baseH, fx_c, fy_c, zoom)
    x = fx_t * baseW * zoom + tx
    y = fy_t * baseH * zoom + ty
    return x, y

def _crop_params(svg_text: str, fx_center: float, fy_center: float,
                 view_w: int, view_h: int, zoom: float) -> Tuple[float, float, float, float, float, float]:
    base_w, base_h = _parse_svg_size(svg_text)
    crop_w = view_w / zoom
    crop_h = view_h / zoom
    cx = fx_center * base_w
    cy = fy_center * base_h
    x0 = max(0.0, min(base_w - crop_w, cx - crop_w / 2))
    y0 = max(0.0, min(base_h - crop_h, cy - crop_h / 2))
    return base_w, base_h, x0, y0, crop_w, crop_h

def project_to_screen_vector(svg_text: str, fx_center: float, fy_center: float,
                             fx_target: float, fy_target: float,
                             view_w: int, view_h: int, zoom: float) -> Tuple[float, float]:
    base_w, base_h, x0, y0, crop_w, crop_h = _crop_params(svg_text, fx_center, fy_center, view_w, view_h, zoom)
    x = ((fx_target * base_w) - x0) / crop_w * view_w
    y = ((fy_target * base_h) - y0) / crop_h * view_h
    return x, y

# -------------------- RENDERERS --------------------
@st.cache_data(show_spinner=False)
def render_crop_data_uri(svg_text: str, fx_center: float, fy_center: float,
                         view_w: int, view_h: int, zoom: float,
                         grayscale: bool) -> str:
    """Vector crop → PNG using CairoSVG. Cached by inputs."""
    base_w, base_h, x0, y0, crop_w, crop_h = _crop_params(svg_text, fx_center, fy_center, view_w, view_h, zoom)
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

    png_bytes = cairosvg.svg2png(bytestring=out.encode("utf-8"),
                                 output_width=view_w,
                                 output_height=view_h)

    if grayscale:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        g = im.convert("L")
        im = Image.merge("RGBA", (g, g, g, im.split()[-1]))
        buf = io.BytesIO(); im.save(buf, format="PNG")
        png_bytes = buf.getvalue()

    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"

def make_map_html_vector(svg_text: str,
                         fx_center: float, fy_center: float,
                         zoom: float, colorize: bool, ring_color: str,
                         overlays: Optional[List[Tuple[float, float, str, float]]] = None) -> str:
    uri = render_crop_data_uri(svg_text, fx_center, fy_center, VIEW_W, VIEW_H, zoom, grayscale=(not colorize))
    r_px = max(RING_PX, 0.010 * min(VIEW_W, VIEW_H))
    overlay_svg = ""
    if overlays:
        overlay_svg = "\n".join(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{rr:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2"/>'
            for (sx, sy, color, rr) in overlays
        )
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

def make_map_html_css(svg_uri: str, baseW: float, baseH: float,
                      fx_center: float, fy_center: float,
                      zoom: float, colorize: bool, ring_color: str,
                      overlays: Optional[List[Tuple[float, float, str, float]]] = None) -> str:
    tx, ty = css_transform(baseW, baseH, fx_center, fy_center, zoom)
    r_px = max(RING_PX, 0.010 * min(VIEW_W, VIEW_H))
    gray_filter = "grayscale(1)" if not colorize else "none"

    overlay_svg = ""
    if overlays:
        overlay_svg = "\n".join(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{rr:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2"/>'
            for (sx, sy, color, rr) in overlays
        )

    # Use an <img> tag for the whole SVG and crop with CSS transform (no blur).
    return f"""
    <div class="map-wrap" style="width:min(100%, {VIEW_W}px); margin:0 auto 6px auto; position:relative;">
      <div style="width:{VIEW_W}px;height:{VIEW_H}px;overflow:hidden;border-radius:14px;position:relative;background:#f6f7f8;">
        <img src="{svg_uri}" alt="map"
             style="position:absolute;left:{tx}px;top:{ty}px;width:{baseW*zoom}px;height:{baseH*zoom}px;
                    image-rendering:crisp-edges;image-rendering:-webkit-optimize-contrast;
                    -webkit-filter:{gray_filter};filter:{gray_filter};" />
      </div>
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
    st.session_state.phase="play"
    st.session_state.history=[]
    st.session_state.remaining=MAX_GUESSES
    st.session_state.won=False
    st.session_state["feedback"] = ""
    rng = random.Random(20250501 + dt.date.today().toordinal()) if st.session_state.mode=="daily" else random.Random()
    choice_name = rng.choice(names)
    st.session_state.answer = by_key[norm(choice_name)]
    return True

# -------------------- STREAMLIT APP --------------------
st.set_page_config(page_title="Tube Guessr", page_icon=None, layout="wide")

# Global CSS
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
    st.session_state.phase="welcome"
    st.session_state.mode="daily"
    st.session_state.answer=None
    st.session_state.remaining=MAX_GUESSES
    st.session_state.history=[]
    st.session_state.won=False
if "feedback" not in st.session_state:
    st.session_state["feedback"] = ""

# Load assets/data
SVG_TEXT = load_svg_text(SVG_PATH)
SVG_URI, SVG_W, SVG_H = load_svg_uri_and_size(SVG_PATH)
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

# Fallback notice (only show once when Cairo is missing)
if not CAIRO_OK and CAIRO_IMPORT_ERROR and "render_notice_shown" not in st.session_state:
    st.info(
        "Vector rasterizer (CairoSVG) isn’t available on this host, so the app is "
        "using the live SVG crop fallback. It stays crisp; if you want server-side "
        "grayscale too, deploy with system Cairo. "
        f"(Import error: {CAIRO_IMPORT_ERROR[:120]}…)"
    )
    st.session_state["render_notice_shown"] = True

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
        st.session_state.phase="start"
        st.rerun()

# -------------------- START --------------------
elif st.session_state.phase == "start":
    st.markdown("# Tube Guessr")
    render_mode_picker(title_on_top=True)
    st.write("")
    if centered_play("Start Game"):
        if start_round(STATIONS, BY_KEY, NAMES): st.rerun()

# -------------------- PLAY / END --------------------
elif st.session_state.phase in ("play","end"):
    st.markdown("# Tube Guessr")
    render_mode_picker(title_on_top=True)

    answer: Station = st.session_state.answer or STATIONS[0]
    colorize=False
    if st.session_state.history:
        last = resolve_guess(st.session_state.history[-1], BY_KEY)
        if last and bool(set(last.lines) & set(answer.lines)): colorize=True

    ring = "#22c55e" if (st.session_state.phase=="end" and st.session_state.won) else ("#eab308" if colorize else "#22c55e")

    # Build overlays
    overlays: List[Tuple[float,float,str,float]] = []
    for gname in st.session_state.history:
        st_obj = BY_KEY.get(norm(gname))
        if not st_obj or st_obj.key == answer.key:
            continue
        if CAIRO_OK:
            sx, sy = project_to_screen_vector(SVG_TEXT, answer.fx, answer.fy, st_obj.fx, st_obj.fy, VIEW_W, VIEW_H, ZOOM)
        else:
            sx, sy = project_to_screen_css(SVG_W, SVG_H, answer.fx, answer.fy, st_obj.fx, st_obj.fy, ZOOM)
        if 0 <= sx <= VIEW_W and 0 <= sy <= VIEW_H:
            color = "#f59e0b" if bool(set(st_obj.lines) & set(answer.lines)) else "#ef4444"
            overlays.append((sx, sy, color, 30.0))

    # Center layout
    _L, mid, _R = st.columns([1,2,1])
    with mid:
        if CAIRO_OK:
            html = make_map_html_vector(SVG_TEXT, answer.fx, answer.fy, ZOOM, colorize, ring, overlays)
        else:
            html = make_map_html_css(SVG_URI, SVG_W, SVG_H, answer.fx, answer.fy, ZOOM, colorize, ring, overlays)
        st.markdown(html, unsafe_allow_html=True)

        if st.session_state.phase == "play":
            q_now = st.text_input(
                "Type to search stations",
                key="live_guess_box",
                placeholder="Start typing… then press Enter",
                label_visibility="collapsed",
            )
            # suggestions directly under map
            names_sorted = NAMES
            if q_now:
                ql = q_now.lower().strip()
                names_sorted = [n for n in NAMES if n.lower().startswith(ql)][:5]
            if names_sorted and q_now:
                st.markdown('<div class="sugg-list">', unsafe_allow_html=True)
                for s in names_sorted:
                    if st.button(s, key=f"sugg_{s}", use_container_width=True):
                        st.session_state.history.append(s)
                        st.session_state.remaining -= 1
                        chosen = BY_KEY.get(norm(s))
                        if chosen and chosen.key == answer.key:
                            st.session_state.won = True
                            st.session_state.phase = "end"
                            st.session_state["feedback"] = ""
                        else:
                            if chosen and bool(set(chosen.lines) & set(answer.lines)):
                                overlap = ", ".join(sorted(set(chosen.lines) & set(answer.lines))) or "right line"
                                st.session_state["feedback"] = f"Wrong station, but correct line ({overlap})."
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
        _l, c, _r = st.columns([1,1,1])
        with c:
            if st.session_state.won:
                st.success("Correct!")
            else:
                st.error(f"Out of guesses. The station was **{answer.name}**.")
            if centered_play("Play again"):
                if start_round(STATIONS, BY_KEY, NAMES): st.rerun()
