# Metrodle Dupe — Public (pixel-accurate inline SVG crop with fragments for smooth typing)
# Live suggestions re-render ONLY the input area via st.fragment (no full-page flicker while typing).
# Big translucent markers (r=30): amber = same line, red = different line, hidden for correct.
# Calibration removed.

import base64
import csv
import datetime as dt
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st

# -------------------- PATHS --------------------
BASE_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = BASE_DIR / "maps"
SVG_PATH = ASSETS_DIR / "tube_map_clean.svg"      # Blank SVG (no labels)
DB_PATH  = BASE_DIR / "stations_db.csv"           # Pre-filled via private calibration

# -------------------- TUNING --------------------
VIEW_W, VIEW_H = 980, 620          # viewport (px) for crop
ZOOM        = 3.0                  # zoom into the answer
RING_PX     = 28                   # center ring radius (px)
RING_STROKE = 6
MAX_GUESSES = 6

# -------------------- DATA --------------------
@dataclass
class Station:
    name: str
    fx: float   # frac X in full SVG (0..1)
    fy: float   # frac Y in full SVG (0..1)
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

# -------------------- ASSETS --------------------
@st.cache_resource(show_spinner=False)
def load_svg_data(svg_path: Path) -> Tuple[str, float, float]:
    """Return (data_uri, baseW, baseH) for the blank map SVG."""
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

# -------------------- GEOMETRY / SVG RENDER --------------------
def css_transform(baseW: float, baseH: float, fx_center: float, fy_center: float, zoom: float) -> Tuple[float, float]:
    """Translate+scale so the answer lands at the viewport center."""
    cx, cy = fx_center * baseW, fy_center * baseH
    tx = VIEW_W / 2 - cx * zoom
    ty = VIEW_H / 2 - cy * zoom
    return tx, ty

def project_to_screen(baseW: float, baseH: float,
                      fx_target: float, fy_target: float,
                      fx_center: float, fy_center: float,
                      zoom: float) -> Tuple[float, float]:
    """Project (fx,fy) into the centered viewport."""
    tx, ty = css_transform(baseW, baseH, fx_center, fy_center, zoom)
    x = fx_target * baseW * zoom + tx
    y = fy_target * baseH * zoom + ty
    return x, y

def make_map_html(svg_uri: str, baseW: float, baseH: float,
                  fx_center: float, fy_center: float,
                  zoom: float, colorize: bool, ring_color: str,
                  overlays: Optional[List[Tuple[float, float, str, float]]] = None) -> str:
    """Inline SVG crop with overlays = [(x,y,color,r), ...] in viewport px."""
    tx, ty = css_transform(baseW, baseH, fx_center, fy_center, zoom)
    r_px = max(RING_PX, 0.010 * min(baseW, baseH) * zoom)

    gray_filter = """
      <filter id="gray">
        <feColorMatrix type="matrix"
          values="0.2126 0.7152 0.0722 0 0
                  0.2126 0.7152 0.0722 0 0
                  0.2126 0.7152 0.0722 0 0
                  0      0      0      1 0"/>
      </filter>
    """
    image_style = 'filter:url(#gray);' if not colorize else ''

    overlay_svg = ""
    if overlays:
        parts = []
        for (sx, sy, color, rr) in overlays:
            parts.append(
                f"""<g class="guess-marker">
                      <circle cx="{sx:.1f}" cy="{sy:.1f}" r="{rr:.1f}"
                              fill="{color}" fill-opacity="0.28"
                              stroke="{color}" stroke-width="2" />
                    </g>"""
            )
        overlay_svg = "\n".join(parts)

    return f"""
    <div class="map-wrap" style="width:min(100%, {VIEW_W}px); margin:0 auto 6px auto;">
      <svg viewBox="0 0 {VIEW_W} {VIEW_H}" width="100%" style="display:block;border-radius:14px;background:#f6f7f8;">
        <defs>{gray_filter}</defs>
        <g transform="translate({tx},{ty}) scale({zoom})">
          <image href="{svg_uri}" width="{baseW}" height="{baseH}" style="{image_style}"/>
        </g>
        <circle cx="{VIEW_W/2}" cy="{VIEW_H/2}" r="{r_px}" stroke="{ring_color}"
                stroke-width="{RING_STROKE}" fill="none"
                style="filter: drop-shadow(0 0 0 rgba(0,0,0,0.45));"/>
        {overlay_svg}
      </svg>
    </div>
    """

# -------------------- STREAMLIT APP --------------------
st.set_page_config(page_title="Metrodle Dupe", page_icon="🗺️", layout="wide")
st.markdown("# Metrodle Dupe")

# Global CSS
st.markdown(
    """
    <style>
      .block-container { max-width: 1100px; padding-top: 2.0rem; padding-bottom: 1rem; }
      .block-container h1:first-of-type { margin: 0 0 .75rem 0; }

      .map-wrap { margin: 0 auto 6px auto !important; }

      .stTextInput { margin-top: 6px !important; margin-bottom: 6px !important; }
      .stTextInput>div>div>input {
        text-align: center; height: 44px; line-height: 44px; font-size: 1rem;
      }

      .sugg-list .stButton>button {
        min-height: 44px; font-size: 1rem; border-radius: 10px; margin-bottom: 6px;
      }

      .post-input { margin-top: 6px; }

      .play-again .stButton>button { font-size: 1.05rem; padding: 12px 22px; border-radius: 10px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Load assets
SVG_URI, SVG_W, SVG_H = load_svg_data(SVG_PATH)

# Session state
if "phase" not in st.session_state:
    st.session_state.phase="start"
    st.session_state.mode="daily"
    st.session_state.answer=None
    st.session_state.remaining=MAX_GUESSES
    st.session_state.history=[]
    st.session_state.won=False
if "feedback" not in st.session_state:
    st.session_state["feedback"] = ""

# Mode picker
c1, _, _ = st.columns([1,1,1])
with c1:
    st.radio("Mode",["daily","practice"],key="mode",horizontal=True)

STATIONS, BY_KEY, NAMES = load_db()

# -------- Game helpers --------
def start_round() -> bool:
    if not STATIONS:
        st.warning("No stations found in stations_db.csv. (Private calibration required.)")
        return False
    st.session_state.phase="play"
    st.session_state.history=[]
    st.session_state.remaining=MAX_GUESSES
    st.session_state.won=False
    st.session_state["feedback"] = ""
    rng = random.Random(20250501 + dt.date.today().toordinal()) if st.session_state.mode=="daily" else random.Random()
    choice_name = rng.choice(NAMES)
    st.session_state.answer = BY_KEY[norm(choice_name)]
    return True

if st.session_state.phase=="start":
    _l, c, _r = st.columns([1,1,1])
    with c:
        if st.button("Start Game", type="primary", use_container_width=True):
            if start_round(): st.rerun()

# -------------------- MAIN PLAY / END --------------------
if st.session_state.phase in ("play", "end") and STATIONS:
    answer: Station = st.session_state.answer or STATIONS[0]
    colorize=False
    if st.session_state.history:
        last = resolve_guess(st.session_state.history[-1], BY_KEY)
        if last and same_line(last, answer): colorize=True
    ring = "#22c55e" if (st.session_state.phase=="end" and st.session_state.won) else ("#eab308" if colorize else "#22c55e")

    # Build overlays for wrong guesses visible in the crop (ANSWER is center)
    overlays: List[Tuple[float,float,str,float]] = []
    for gname in st.session_state.history:
        st_obj = resolve_guess(gname, BY_KEY)
        if not st_obj or st_obj.key == answer.key:  # hide ring for correct
            continue
        sx, sy = project_to_screen(SVG_W, SVG_H, st_obj.fx, st_obj.fy, answer.fx, answer.fy, ZOOM)
        if 0 <= sx <= VIEW_W and 0 <= sy <= VIEW_H:
            color = "#f59e0b" if same_line(st_obj, answer) else "#ef4444"
            overlays.append((sx, sy, color, 30.0))  # 3× diameter-ish

    _L, mid, _R = st.columns([1,2,1])
    with mid:
        # Map (does NOT re-render while typing; see fragment below)
        st.markdown(
            make_map_html(SVG_URI, SVG_W, SVG_H, answer.fx, answer.fy, ZOOM, colorize, ring, overlays),
            unsafe_allow_html=True
        )

        # --------- FRAGMENT: only this input + suggestions area reruns on keystrokes ---------
        try:
            fragment = st.fragment  # Streamlit >= 1.32
        except AttributeError:
            def fragment(func=None, *fargs, **fkwargs):
                def deco(f): return f
                return deco if func is None else deco(func)

        @fragment
        def guess_fragment(names, by_key, answer_obj):
            # IMPORTANT: never programmatically set/delete this key elsewhere
            q_now = st.text_input(
                "Type to search stations",
                key="live_guess_box",
                placeholder="Start typing…",
                label_visibility="collapsed",
            )

            sugg = prefix_suggestions(q_now or "", names, limit=5)
            picked = None
            if sugg:
                st.markdown('<div class="sugg-list">', unsafe_allow_html=True)
                for s in sugg:
                    if st.button(s, key=f"sugg_{s}", use_container_width=True):
                        picked = s
                        break
                st.markdown("</div>", unsafe_allow_html=True)

            if picked:
                st.session_state.history.append(picked)
                st.session_state.remaining -= 1
                chosen = resolve_guess(picked, by_key)
                if chosen and chosen.key == answer_obj.key:
                    st.session_state.won = True
                    st.session_state.phase = "end"
                    st.session_state["feedback"] = ""
                else:
                    if chosen and same_line(chosen, answer_obj):
                        lines = ", ".join(overlap_lines(chosen, answer_obj)) or "right line"
                        st.session_state["feedback"] = f"❌ Wrong station, but correct line ({lines})."
                    else:
                        st.session_state["feedback"] = "❌ Wrong station."
                    if st.session_state.remaining <= 0:
                        st.session_state.won = False
                        st.session_state.phase = "end"
                st.rerun()  # one full rerun to refresh map/overlays/history

        if st.session_state.phase == "play":
            guess_fragment(NAMES, BY_KEY, answer)

        # Feedback + history/status
        if st.session_state.get("feedback"):
            st.info(st.session_state["feedback"])
        if st.session_state.history:
            st.markdown('<div class="post-input">**Your guesses:** ' + ", ".join(st.session_state.history) + "</div>", unsafe_allow_html=True)
        st.caption(f"Guesses left: {st.session_state.remaining}")

    # End screen
    if st.session_state.phase == "end":
        _l, c, _r = st.columns([1,1,1])
        with c:
            if st.session_state.won:
                st.success("Nice! You got it.")
            else:
                st.error(f"Out of guesses. The station was **{answer.name}**.")
            st.markdown('<div class="play-again">', unsafe_allow_html=True)
            if st.button("Play again", type="primary", use_container_width=True):
                if start_round(): st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
