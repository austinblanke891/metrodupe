# Tube Guessr — inline SVG (no iframe), classic centered crop, mobile-friendly, greyscale works on iOS
# The map is an inline <svg> using <image> + feColorMatrix. Crop math unchanged; input sits flush underneath.

import base64
import csv
import datetime as dt
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st

# ---------- fragment polyfill ----------
try:
    st_fragment = st.fragment
except AttributeError:
    try:
        st_fragment = st.experimental_fragment
    except AttributeError:
        def st_fragment(func=None, **kwargs):
            if func is None:
                def _wrap(f): return f
                return _wrap
            return func
# ---------------------------------------

# -------------------- PATHS --------------------
BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "maps"
SVG_PATH = ASSETS_DIR / "tube_map_clean.svg"      # Blank SVG (no labels)
DB_PATH  = BASE_DIR / "stations_db.csv"           # Pre-filled via private calibration

# -------------------- TUNING --------------------
VIEW_W, VIEW_H = 980, 620
ZOOM        = 3.0
RING_PX     = 28
RING_STROKE = 6
MAX_GUESSES = 6

# -------------------- GLOBAL CSS --------------------
st.set_page_config(page_title="Tube Guessr", page_icon=None, layout="wide")
st.markdown(
    """
    <style>
      .block-container { max-width: 1100px; padding-top: 2.6rem; padding-bottom: .6rem; }
      @media (max-width: 900px){ .block-container { padding-top: 3.2rem; } }
      .block-container h1:first-of-type { margin: 0 0 .5rem 0; }

      /* Remove default vertical gaps */
      section.main div[data-testid="stVerticalBlock"] { row-gap: 0 !important; }
      section.main div.element-container { margin-bottom: 0 !important; padding-bottom: 0 !important; }
      section.main div[data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }

      /* Radios */
      div[data-baseweb="radio"] label { font-size: 1rem; margin-right: 1rem; }

      /* Buttons */
      .stButton>button {
        min-height: 44px; font-size: 1rem; border-radius: 9999px; padding: 10px 18px;
        background: #2563eb; color: #fff; border: none;
      }
      .stButton>button:hover { background: #1d4ed8; }
      .play-center { display:flex; justify-content:center; }
      .play-center .stButton>button { min-width: 220px; }

      /* Map + input: zero gap */
      .map-wrap { width:min(100%, 980px); margin:0 auto 0 auto !important; }
      .map-wrap svg { display:block; width:100%; height:auto; border-radius:14px; background:#0f1115; }
      .guess-wrap { width:min(100%, 980px); margin:0 auto; padding: 0 !important; }

      .stTextInput { margin-top: 0 !important; margin-bottom: 0 !important; }
      .stTextInput>div>div>input {
        text-align: center; height: 44px; line-height: 44px; font-size: 1rem; border-radius: 10px;
      }

      /* Suggestions with a touch more separation */
      .sugg-list { margin-top: 8px; }
      .sugg-list div.element-container { margin-bottom: 12px !important; }
      .sugg-list .stButton>button {
        width: 100%;
        border-radius: 14px;
        box-shadow: 0 0 0 1px rgba(255,255,255,.12) inset;
        padding: 12px 16px;
      }

      .post-input { margin-top: 8px; font-size: .95rem; }

      /* Result cards */
      .card { border-radius: 12px; padding: 14px 16px; margin-top: 8px; }
      .card.success { background:#0f2e20; border:1px solid #14532d; color:#dcfce7; }
      .card.error   { background:#2a1313; border:1px solid #7f1d1d; color:#fee2e2; }
    </style>
    """,
    unsafe_allow_html=True,
)

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

@st.cache_data(show_spinner=False)
def load_db() -> Tuple[List[Station], Dict[str, Station], List[str]]:
    ensure_db()
    stations: List[Station] = []
    with open(DB_PATH, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for r in rdr:
            try:
                name = clean_display(r["name"])
                fx = float(r["fx"]); fy = float(r["fy"])
                lines = normalize_lines(r.get("lines", "").split(";"))
                if 0 <= fx <= 1 and 0 <= fy <= 1 and name:
                    stations.append(Station(name, fx, fy, lines))
            except Exception:
                continue
    by_key = {s.key: s for s in stations}
    return stations, by_key, sorted([s.name for s in stations])

# -------------------- ASSETS (b64 data-uri for <image>) --------------------
@st.cache_resource(show_spinner=False)
def load_svg_datauri(svg_path: Path) -> Tuple[str, float, float]:
    if not svg_path.exists():
        raise FileNotFoundError(f"SVG not found: {svg_path}")
    raw = svg_path.read_bytes()
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

# -------------------- GEOMETRY --------------------
def css_transform(baseW: float, baseH: float, fx_center: float, fy_center: float, zoom: float) -> Tuple[float, float]:
    cx, cy = fx_center * baseW, fy_center * baseH
    tx = VIEW_W / 2 - cx * zoom
    ty = VIEW_H / 2 - cy * zoom
    return tx, ty

def project_to_screen_precomputed(baseW: float, baseH: float, tx: float, ty: float, zoom: float, fx: float, fy: float) -> Tuple[float, float]:
    x = fx * baseW * zoom + tx
    y = fy * baseH * zoom + ty
    return x, y

# -------------------- MAP (inline SVG) --------------------
def make_map_html_inline(svg_data_uri: str, baseW: float, baseH: float,
                         tx: float, ty: float, zoom: float, colorize: bool, ring_color: str,
                         overlays: Optional[List[Tuple[float, float, str, float]]] = None) -> str:
    """Returns inline HTML for the map. Responsive width; aspect from viewBox; no iframe; no gaps."""
    r_px = max(RING_PX, 0.010 * min(baseW, baseH) * zoom)
    filter_attr = '' if colorize else 'filter="url(#gray)"'

    overlay_svg = ""
    if overlays:
        overlay_svg = "\n".join(
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="{rr:.1f}" '
            f'fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2" />'
            for (sx, sy, color, rr) in overlays
        )

    return f"""
    <div class="map-wrap">
      <svg viewBox="0 0 {VIEW_W} {VIEW_H}" preserveAspectRatio="xMidYMid meet">
        <defs>
          <filter id="gray">
            <feColorMatrix type="matrix"
              values="0.2126 0.7152 0.0722 0 0
                      0.2126 0.7152 0.0722 0 0
                      0.2126 0.7152 0.0722 0 0
                      0      0      0      1 0"/>
          </filter>
        </defs>

        <!-- Map image positioned by translate+scale, then grey-filtered -->
        <g transform="translate({tx},{ty}) scale({zoom})">
          <image href="{svg_data_uri}" width="{baseW}" height="{baseH}" {filter_attr}/>
        </g>

        <!-- Center ring -->
        <circle cx="{VIEW_W/2}" cy="{VIEW_H/2}" r="{r_px}"
                stroke="{ring_color}" stroke-width="{RING_STROKE}" fill="none"/>

        <!-- Wrong-guess markers -->
        {overlay_svg}
      </svg>
    </div>
    """

# -------------------- CARDS --------------------
def success_card(text: str) -> str:
    return f'<div class="card success">{text}</div>'

def error_card(text: str) -> str:
    return f'<div class="card error">{text}</div>'

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

def render_mode_picker(title_on_top=False):
    if title_on_top:
        st.markdown("### Mode")
    choice = st.radio(
        label="Mode",
        options=["daily", "practice"],
        index=(0 if st.session_state.mode == "daily" else 1),
        horizontal=True,
        label_visibility="collapsed",
        key="mode_radio"
    )
    st.session_state.mode = choice

def centered_play(label, key=None, top_margin_px: int = 0):
    st.markdown(f'<div class="play-center" style="margin-top:{top_margin_px}px;">', unsafe_allow_html=True)
    clicked = st.button(label, type="primary", key=key)
    st.markdown('</div>', unsafe_allow_html=True)
    return clicked

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
    q = (q or "").strip().lower()
    if not q:
        return []
    matches = [n for n in names if n.lower().startswith(q)]
    return sorted(matches)[:limit]

# -------------------- FRAGMENT: PLAY AREA --------------------
@st_fragment
def play_fragment(answer: 'Station', stations, by_key, names, svg_data_uri, svg_w, svg_h):
    colorize = False
    if st.session_state.history:
        last = resolve_guess(st.session_state.history[-1], by_key)
        if last and same_line(last, answer):
            colorize = True
    ring = "#22c55e" if (st.session_state.phase=="end" and st.session_state.won) else ("#eab308" if colorize else "#22c55e")

    tx, ty = css_transform(svg_w, svg_h, answer.fx, answer.fy, ZOOM)

    overlays: List[Tuple[float,float,str,float]] = []
    for gname in st.session_state.history:
        st_obj = resolve_guess(gname, by_key)
        if not st_obj or st_obj.key == answer.key:
            continue
        sx, sy = project_to_screen_precomputed(svg_w, svg_h, tx, ty, ZOOM, st_obj.fx, st_obj.fy)
        if 0 <= sx <= VIEW_W and 0 <= sy <= VIEW_H:
            color = "#f59e0b" if same_line(st_obj, answer) else "#ef4444"
            overlays.append((sx, sy, color, 30.0))

    _L, mid, _R = st.columns([1,2,1])
    with mid:
        # Inline SVG map (responsive). No iframe → no spacing issues.
        html = make_map_html_inline(svg_data_uri, svg_w, svg_h, tx, ty, ZOOM, colorize, ring, overlays)
        st.markdown(html, unsafe_allow_html=True)

        # Guess input immediately under the map
        st.markdown('<div class="guess-wrap">', unsafe_allow_html=True)
        if st.session_state.phase == "play":
            q_now = st.text_input(
                "Type to search stations",
                key="live_guess_box",
                placeholder="Start typing… then press Enter",
                label_visibility="collapsed",
            )
            sugg = prefix_suggestions(q_now or "", names, limit=5)
            if sugg:
                st.markdown('<div class="sugg-list">', unsafe_allow_html=True)
                for s in sugg:
                    if st.button(s, key=f"sugg_{s}", use_container_width=True):
                        st.session_state.history.append(s)
                        st.session_state.remaining -= 1
                        chosen = resolve_guess(s, by_key)
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
        st.markdown('</div>', unsafe_allow_html=True)

        if st.session_state.get("feedback"):
            st.info(st.session_state["feedback"])
        if st.session_state.history:
            st.markdown('<div class="post-input">**Your guesses:** ' + ", ".join(st.session_state.history) + "</div>", unsafe_allow_html=True)
        st.caption(f"Guesses left: {st.session_state.remaining}")

    if st.session_state.phase == "end":
        _l, c, _r = st.columns([1,1,1])
        with c:
            if st.session_state.won:
                st.markdown('<div class="card success">Correct!</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="card error">Out of guesses. The station was <b>{answer.name}</b>.</div>', unsafe_allow_html=True)
            if centered_play("Play again", key="play_again_btn", top_margin_px=16):
                if start_round(stations, by_key, names): st.rerun()

# -------------------- SESSION & APP --------------------
if "phase" not in st.session_state:
    st.session_state.phase="welcome"
    st.session_state.mode="daily"
    st.session_state.answer=None
    st.session_state.remaining=MAX_GUESSES
    st.session_state.history=[]
    st.session_state.won=False
if "feedback" not in st.session_state:
    st.session_state["feedback"] = ""

SVG_DATA_URI, SVG_W, SVG_H = load_svg_datauri(SVG_PATH)
STATIONS, BY_KEY, NAMES = load_db()

def centered_play(label, key=None, top_margin_px: int = 0):
    st.markdown(f'<div class="play-center" style="margin-top:{top_margin_px}px;">', unsafe_allow_html=True)
    clicked = st.button(label, type="primary", key=key)
    st.markdown('</div>', unsafe_allow_html=True)
    return clicked

def render_mode_picker(title_on_top=False):
    if title_on_top:
        st.markdown("### Mode")
    choice = st.radio(
        label="Mode",
        options=["daily", "practice"],
        index=(0 if st.session_state.mode == "daily" else 1),
        horizontal=True,
        label_visibility="collapsed",
        key="mode_radio"
    )
    st.session_state.mode = choice

if st.session_state.phase == "welcome":
    st.markdown("# Tube Guessr")
    st.markdown(
        """
        Guess the London Underground station from a zoomed-in crop of the Tube map.

        **How to play**
        - Start typing a station name in the search box on the game screen, then press Enter.
        - A list of auto-fill suggestions will appear — click one to submit your guess.
        - If your guess is wrong but on the correct line, we’ll tell you.
        - You have 6 guesses.
        """
    )
    st.divider()
    if centered_play("Play", key="welcome_play_btn"):
        st.session_state.phase="start"; st.rerun()

elif st.session_state.phase == "start":
    st.markdown("# Tube Guessr")
    with st.form("mode_pick", clear_on_submit=False):
        render_mode_picker(title_on_top=True)
        submitted = st.form_submit_button("Start Game")
    if submitted:
        if start_round(STATIONS, BY_KEY, NAMES): st.rerun()

elif st.session_state.phase in ("play","end"):
    st.markdown("# Tube Guessr")
    with st.container(): render_mode_picker(title_on_top=True)
    answer: Station = st.session_state.answer or (STATIONS[0] if STATIONS else Station("?", 0.5, 0.5, []))
    play_fragment(answer, STATIONS, BY_KEY, NAMES, SVG_DATA_URI, SVG_W, SVG_H)

else:
    st.session_state.phase = "welcome"
    st.experimental_rerun()
