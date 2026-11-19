"""Shared dark, square-edged "ops console" theme (Anduril / Palantir style).

Neutral graphite surfaces, hairline borders, one restrained steel-blue accent,
uppercase tracked micro-labels, tight density, square corners. Call
apply_theme() first in every @ui.page builder.
"""
from nicegui import ui

BG = "#0d0f0d"        # page
SURFACE = "#141714"   # cards / panels
RAISED = "#1a1e1a"    # headers, inputs, table heads
BORDER = "#2c322c"
TEXT = "#d7dbe2"
MUTED = "#8d968b"
ACCENT = "#d0a644"

# mission timeline palette. A step block is coloured by what it does, not
# by alarm: red is kept for conflicts and errors alone. Each kind has a fill,
# an edge (the fill about 15 % lighter) and its label colour.
TIMELINE_TEXT = "#F2F4EF"
TIMELINE_KINDS = {
    "tx":        {"fill": "#8A6A1E", "border": "#B8902F", "text": TIMELINE_TEXT},  # radiating
    "rx":        {"fill": "#3F5F73", "border": "#5E8499", "text": TIMELINE_TEXT},  # listen, record
    "playback":  {"fill": "#5B6B2E", "border": "#7E9142", "text": TIMELINE_TEXT},  # IQ playback, replay
    "comm":      {"fill": "#4E5A7A", "border": "#6F7DA3", "text": TIMELINE_TEXT},  # scenario waveforms
    "jammer":    {"fill": "#7A4E2D", "border": "#A06E45", "text": TIMELINE_TEXT},
    "generator": {"fill": "#5A5F66", "border": "#7C828A", "text": TIMELINE_TEXT},  # tone, chirp, noise
    "config":    {"fill": "#2F3630", "border": "#4A544B", "text": "#C9CFC4"},      # set rate, dwell
}
# catalog group -> kind; a technique entry overrides its group (the record and
# replay group holds one of each)
TIMELINE_GROUP_KIND = {"Jammers": "jammer", "Tools generators": "generator",
                       "Radio setup": "config", "IQ playback": "playback",
                       "Comms": "comm", "Transponder": "tx", "Secure": "tx"}
TIMELINE_TECHNIQUE_KIND = {"record": "rx", "replay": "playback"}
TIMELINE_PRIMARY = TIMELINE_TEXT       # 2 px edge of the step the editor shows
TIMELINE_SELECTED = ACCENT             # edge of the other selected steps, and the ring
TIMELINE_CONFLICT = "#E5484D"          # the only red: a step with a plan error
TIMELINE_CONFLICT_RING = "#E5484D33"
TIMELINE_STAGED_BORDER = "#6A7560"     # dashed edge of a planning-bench chip
TIMELINE_NOW = "#7FA650"               # the current-time line on the UTC day


def timeline_kind(technique: str, group: str = "", radiates: bool = False) -> str:
    """The palette kind for a step: by technique, then catalog group, else
    radiating (tx) or not (rx)."""
    return (TIMELINE_TECHNIQUE_KIND.get(technique) or TIMELINE_GROUP_KIND.get(group)
            or ("tx" if radiates else "rx"))


TIMELINE_CSS = f"""
.mp-root .mp-step:hover {{ filter:brightness(1.08); border-color:{ACCENT} !important; z-index:6; }}
.mp-root .mp-step-offline {{ opacity:.55; background-image:repeating-linear-gradient(45deg,
    rgba(0,0,0,.30) 0 5px, transparent 5px 10px) !important; }}
.mp-root .mp-step-badge {{ position:absolute; top:3px; right:3px; width:14px; height:14px;
    background:{TIMELINE_CONFLICT}; color:#fff; font:700 10px/14px 'JetBrains Mono',Menlo,monospace;
    text-align:center; pointer-events:none; z-index:2; }}
.mp-root .mp-bench-chip {{ opacity:.7; border:1px dashed {TIMELINE_STAGED_BORDER} !important; }}
.mp-root .mp-bench-chip:hover {{ opacity:1; }}
"""

THEME_CSS = f"""
:root {{ --bg:{BG}; --surface:{SURFACE}; --raised:{RAISED}; --border:{BORDER};
  --text:{TEXT}; --muted:{MUTED}; --accent:{ACCENT}; }}
html, body, .q-layout, .q-page, .nicegui-content {{ background:var(--bg) !important; color:var(--text); }}
body, .q-field, .q-btn, .q-tab {{ font-family:'Inter','SF Pro Text','Helvetica Neue',Arial,sans-serif;
  font-size:13px; letter-spacing:.01em; }}
.font-mono, code, pre {{ font-family:'JetBrains Mono','SF Mono',Menlo,Consolas,monospace !important; }}
.q-card{{ gap:.5rem; }}

/* square edges everywhere, except deliberate dots/avatars */
*, *::before, *::after {{ border-radius:0 !important; }}
.rounded-full, .q-spinner, .q-linear-progress__track, .q-radio__inner, .q-radio__bg {{ border-radius:9999px !important; }}
.leaflet-container *, .maplibregl-map * {{ border-radius:0 !important; }}

/* surfaces */
.q-card, .q-menu, .q-dialog__inner > .q-card, .q-table__card, .q-expansion-item,
.q-tab-panels, .q-tab-panel {{ background:var(--surface) !important; color:var(--text) !important; }}
.q-card {{ border:1px solid var(--border); box-shadow:none !important; }}
.q-tab-panel {{ padding:12px; }}
.q-header {{ background:var(--raised) !important; border-bottom:1px solid var(--border); border-top:2px solid #6b7a3a; font-family:'JetBrains Mono','SF Mono',Menlo,monospace;
  box-shadow:none !important; text-transform:uppercase; letter-spacing:.08em; }}
.q-header .text-lg {{ font-size:13px; font-weight:600; }}
.q-header a {{ color:var(--muted) !important; text-decoration:none; font-size:11px; }}
.q-header a:hover {{ color:var(--accent) !important; }}
.q-tabs {{ background:var(--raised); border-bottom:1px solid var(--border); color:var(--muted); }}
.q-tab {{ text-transform:uppercase; letter-spacing:.08em; font-size:11px; font-weight:600; min-height:36px; }}
.q-tab--active {{ color:var(--accent) !important; }}
.q-tab__indicator {{ height:2px; background:var(--accent) !important; }}
.border, .q-expansion-item, .q-expansion-item__container {{ border-color:var(--border) !important; }}
.q-separator {{ background:var(--border) !important; }}
.q-item__label--caption, .q-field__label, .q-field__hint {{ color:var(--muted) !important; }}

/* buttons & fields */
.q-btn {{ text-transform:uppercase; letter-spacing:.06em; font-size:11px; font-weight:600; box-shadow:none !important; }}
.q-btn.bg-primary, .q-btn--standard.bg-primary {{ background:var(--accent) !important; color:#0b0d0b !important; }}
.q-btn--flat, .q-btn--outline {{ color:var(--accent); }}
.q-field--outlined .q-field__control:before {{ border-color:var(--border); }}
.q-field--outlined .q-field__control {{ background:var(--raised); }}
.q-field--filled .q-field__control {{ background:var(--raised) !important; }}
.q-field--dark .q-field__native, .q-field__native, .q-field__input {{ color:var(--text); }}

/* fields in every state. Quasar paints a field's fill from bg-color (a
   light palette tint on a dark page, so the white text vanished) and greys a
   focused/readonly/disabled one with its light defaults; hold all of them on
   the dark palette. An edited, not yet applied field is marked with mp-dirty. */
.q-field .q-field__control, .q-field .q-field__control[class*="bg-"] {{ background:var(--raised) !important; }}
.q-field--standout .q-field__control, .q-field--focused .q-field__control {{ background:var(--raised) !important; }}
.q-field--outlined.q-field--focused .q-field__control:after {{ border-color:var(--accent) !important; border-width:1px; }}
.q-field--outlined .q-field__control:hover:before {{ border-color:#4a5340; }}
.q-field__native, .q-field__input, .q-field__prefix, .q-field__suffix, .q-field input, .q-field textarea,
.q-select .q-field__native span, .q-field--focused .q-field__native {{ color:var(--text) !important; -webkit-text-fill-color:var(--text); }}
.q-field__marginal, .q-select__dropdown-icon, .q-field__append .q-icon {{ color:var(--muted) !important; }}
.q-field--focused .q-field__label {{ color:var(--accent) !important; }}
.q-field input::placeholder, .q-field textarea::placeholder {{ color:#6c7468 !important; -webkit-text-fill-color:#6c7468; opacity:1; }}
.q-field--readonly .q-field__control, .q-field--disabled .q-field__control {{ background:#111411 !important; }}
.q-field--disabled .q-field__native, .q-field--disabled .q-field__label {{ color:var(--muted) !important; -webkit-text-fill-color:var(--muted); }}
.q-field--error .q-field__control:after {{ border-color:#e5484d !important; }}
.q-field.mp-dirty .q-field__control {{ background:#2b2512 !important; box-shadow:inset 0 0 0 1px var(--accent); }}
.q-field input[type=number], input, textarea, select {{ color-scheme:dark; }}
input:-webkit-autofill {{ -webkit-box-shadow:0 0 0 40px var(--raised) inset !important; -webkit-text-fill-color:var(--text); }}
.q-checkbox__inner, .q-radio__inner, .q-toggle__inner {{ color:var(--muted); }}
.q-checkbox__inner--truthy, .q-radio__inner--truthy {{ color:var(--accent) !important; }}

/* dropdowns, menus, dialogs, tooltips, notifications */
.q-menu, .q-select__dialog, .q-dialog .q-card, .q-uploader, .q-date, .q-time {{ background:var(--surface) !important;
  color:var(--text) !important; border:1px solid var(--border); }}
.q-item {{ color:var(--text); }}
.q-item.q-manual-focusable--focused > .q-focus-helper, .q-item:hover > .q-focus-helper {{ background:#262b20 !important; opacity:1 !important; }}
.q-item--active, .q-menu .q-item.text-primary {{ color:var(--accent) !important; background:#22261b; }}
.q-virtual-scroll__content .q-item {{ min-height:30px; }}
.q-tooltip {{ background:#22281f !important; color:var(--text) !important; border:1px solid var(--border); font-size:11px; }}
.q-dialog__backdrop {{ background:rgba(0,0,0,.6) !important; }}
.q-expansion-item__toggle-icon, .q-expansion-item .q-item__section--side .q-icon {{ color:var(--muted); }}
.nicegui-code, .q-uploader__list, .q-uploader__header {{ background:#0f120f !important; color:var(--text); }}
.q-badge {{ color:#0b0d0b; }}
.q-btn-group--outline > .q-btn-item:not(:last-child) {{ border-right:1px solid var(--border); }}

/* tables */
.q-table, .q-table__container {{ background:transparent !important; color:var(--text); }}
.q-table thead tr th, .sticky-head thead tr th {{ background:var(--raised) !important; color:var(--muted);
  text-transform:uppercase; letter-spacing:.06em; font-size:10.5px; border-color:var(--border) !important; }}
.q-table td {{ border-color:var(--border) !important; }}
.q-table tbody tr:hover td {{ background:#1f241f; }}
.q-table tbody tr.selected td {{ background:#33300f !important; }}

/* remap light tailwind utilities used in the app onto the dark palette */
.bg-white, .bg-gray-50, .bg-gray-100, .bg-slate-50, .bg-slate-100, .bg-grey-1, .bg-grey-2 {{ background:var(--surface) !important; }}
.bg-gray-200, .bg-slate-200, .bg-gray-300 {{ background:var(--raised) !important; }}
.bg-slate-700, .bg-slate-800, .bg-slate-900 {{ background:var(--raised) !important; }}
.bg-amber-100, .bg-yellow-100, .bg-orange-100 {{ background:#3a2f12 !important; }}
.bg-red-100, .bg-red-50 {{ background:#3a1719 !important; }}
.bg-green-100, .bg-green-50, .bg-emerald-100 {{ background:#12301e !important; }}
.bg-blue-100, .bg-blue-50, .bg-sky-100 {{ background:#262a14 !important; }}
.text-black, .text-gray-900, .text-gray-800, .text-gray-700, .text-slate-800, .text-slate-700,
.text-grey-9, .text-grey-8 {{ color:var(--text) !important; }}
.text-gray-600, .text-gray-500, .text-gray-400, .text-slate-600, .text-slate-500, .text-grey-7, .text-grey-6 {{ color:var(--muted) !important; }}
.text-amber-900, .text-amber-800, .text-yellow-800, .text-orange-800 {{ color:#f0c36a !important; }}
.text-red-900, .text-red-800, .text-red-700, .text-red-600 {{ color:#ff8a8f !important; }}
.text-green-900, .text-green-800, .text-green-700, .text-green-600 {{ color:#6fd08c !important; }}
.text-blue-900, .text-blue-800, .text-blue-700, .text-blue-600 {{ color:var(--accent) !important; }}
.border-gray-200, .border-gray-300, .border-slate-200, .border-slate-300 {{ border-color:var(--border) !important; }}

/* Quasar's light palette tints (bg-*-1..4, text-*-7..10) as used by chips, pills
   and field fills: dark tints with light text, per hue */
.bg-red-1, .bg-red-2, .bg-red-3, .bg-red-4 {{ background:#3a1719 !important; }}
.bg-pink-1, .bg-pink-2, .bg-pink-3, .bg-pink-4 {{ background:#3a1729 !important; }}
.bg-purple-1, .bg-purple-2, .bg-purple-3, .bg-purple-4 {{ background:#2c1a3a !important; }}
.bg-deep-purple-1, .bg-deep-purple-2, .bg-deep-purple-3, .bg-deep-purple-4 {{ background:#261a3a !important; }}
.bg-indigo-1, .bg-indigo-2, .bg-indigo-3, .bg-indigo-4 {{ background:#1c2036 !important; }}
.bg-blue-1, .bg-blue-2, .bg-blue-3, .bg-blue-4 {{ background:#1c2433 !important; }}
.bg-light-blue-1, .bg-light-blue-2, .bg-light-blue-3, .bg-light-blue-4 {{ background:#1a2a36 !important; }}
.bg-cyan-1, .bg-cyan-2, .bg-cyan-3, .bg-cyan-4 {{ background:#14303a !important; }}
.bg-teal-1, .bg-teal-2, .bg-teal-3, .bg-teal-4 {{ background:#12302c !important; }}
.bg-green-1, .bg-green-2, .bg-green-3, .bg-green-4 {{ background:#12301e !important; }}
.bg-light-green-1, .bg-light-green-2, .bg-light-green-3, .bg-light-green-4 {{ background:#1f3014 !important; }}
.bg-lime-1, .bg-lime-2, .bg-lime-3, .bg-lime-4 {{ background:#2a3012 !important; }}
.bg-yellow-1, .bg-yellow-2, .bg-yellow-3, .bg-yellow-4 {{ background:#36310f !important; }}
.bg-amber-1, .bg-amber-2, .bg-amber-3, .bg-amber-4 {{ background:#3a2f12 !important; }}
.bg-orange-1, .bg-orange-2, .bg-orange-3, .bg-orange-4 {{ background:#3a2812 !important; }}
.bg-deep-orange-1, .bg-deep-orange-2, .bg-deep-orange-3, .bg-deep-orange-4 {{ background:#3a2014 !important; }}
.bg-brown-1, .bg-brown-2, .bg-brown-3, .bg-brown-4 {{ background:#2f2419 !important; }}
.bg-grey-1, .bg-grey-2, .bg-grey-3, .bg-grey-4 {{ background:#1a1e1a !important; }}
.bg-blue-grey-1, .bg-blue-grey-2, .bg-blue-grey-3, .bg-blue-grey-4 {{ background:#1d2327 !important; }}
.text-red-7, .text-red-8, .text-red-9, .text-red-10 {{ color:#ff8a8f !important; }}
.text-pink-7, .text-pink-8, .text-pink-9, .text-pink-10 {{ color:#ff8ac0 !important; }}
.text-purple-7, .text-purple-8, .text-purple-9, .text-purple-10 {{ color:#cf9cff !important; }}
.text-deep-purple-7, .text-deep-purple-8, .text-deep-purple-9, .text-deep-purple-10 {{ color:#b9a0ff !important; }}
.text-indigo-7, .text-indigo-8, .text-indigo-9, .text-indigo-10 {{ color:#9fb0ff !important; }}
.text-blue-7, .text-blue-8, .text-blue-9, .text-blue-10 {{ color:#8fbfff !important; }}
.text-light-blue-7, .text-light-blue-8, .text-light-blue-9, .text-light-blue-10 {{ color:#8fd3ff !important; }}
.text-cyan-7, .text-cyan-8, .text-cyan-9, .text-cyan-10 {{ color:#7fe0ef !important; }}
.text-teal-7, .text-teal-8, .text-teal-9, .text-teal-10 {{ color:#6fd0c0 !important; }}
.text-green-7, .text-green-8, .text-green-9, .text-green-10 {{ color:#6fd08c !important; }}
.text-light-green-7, .text-light-green-8, .text-light-green-9, .text-light-green-10 {{ color:#a6d870 !important; }}
.text-lime-7, .text-lime-8, .text-lime-9, .text-lime-10 {{ color:#d0e070 !important; }}
.text-yellow-7, .text-yellow-8, .text-yellow-9, .text-yellow-10 {{ color:#f0dc6a !important; }}
.text-amber-7, .text-amber-8, .text-amber-9, .text-amber-10 {{ color:#f0c36a !important; }}
.text-orange-7, .text-orange-8, .text-orange-9, .text-orange-10 {{ color:#f0a860 !important; }}
.text-deep-orange-7, .text-deep-orange-8, .text-deep-orange-9, .text-deep-orange-10 {{ color:#f08a60 !important; }}
.text-brown-7, .text-brown-8, .text-brown-9, .text-brown-10 {{ color:#d0a888 !important; }}
.text-grey-7, .text-grey-8, .text-grey-9, .text-grey-10 {{ color:#d7dbe2 !important; }}
.text-blue-grey-7, .text-blue-grey-8, .text-blue-grey-9, .text-blue-grey-10 {{ color:#b0c0cc !important; }}

.q-chip.bg-grey-3, .q-chip[class*=bg-], .q-chip.bg-green-1, .q-chip.bg-grey-2, .q-chip.bg-grey-4 {{ background:var(--raised) !important; color:var(--text) !important; border:1px solid var(--border); }}
.q-chip.bg-green-1 {{ color:#6fd08c !important; }}
/* a light tinted chip (an error count, a warning) keeps its hue on the dark tint */
.q-chip.bg-red-1, .q-chip.bg-red-2, .q-chip.bg-red-3, .q-chip.bg-red-4 {{ background:#3a1719 !important; color:#ff8a8f !important; border-color:#ff8a8f55; }}
.q-chip.bg-pink-1, .q-chip.bg-pink-2, .q-chip.bg-pink-3, .q-chip.bg-pink-4 {{ background:#3a1729 !important; color:#ff8ac0 !important; border-color:#ff8ac055; }}
.q-chip.bg-purple-1, .q-chip.bg-purple-2, .q-chip.bg-purple-3, .q-chip.bg-purple-4 {{ background:#2c1a3a !important; color:#cf9cff !important; border-color:#cf9cff55; }}
.q-chip.bg-deep-purple-1, .q-chip.bg-deep-purple-2, .q-chip.bg-deep-purple-3, .q-chip.bg-deep-purple-4 {{ background:#261a3a !important; color:#b9a0ff !important; border-color:#b9a0ff55; }}
.q-chip.bg-indigo-1, .q-chip.bg-indigo-2, .q-chip.bg-indigo-3, .q-chip.bg-indigo-4 {{ background:#1c2036 !important; color:#9fb0ff !important; border-color:#9fb0ff55; }}
.q-chip.bg-blue-1, .q-chip.bg-blue-2, .q-chip.bg-blue-3, .q-chip.bg-blue-4 {{ background:#1c2433 !important; color:#8fbfff !important; border-color:#8fbfff55; }}
.q-chip.bg-light-blue-1, .q-chip.bg-light-blue-2, .q-chip.bg-light-blue-3, .q-chip.bg-light-blue-4 {{ background:#1a2a36 !important; color:#8fd3ff !important; border-color:#8fd3ff55; }}
.q-chip.bg-cyan-1, .q-chip.bg-cyan-2, .q-chip.bg-cyan-3, .q-chip.bg-cyan-4 {{ background:#14303a !important; color:#7fe0ef !important; border-color:#7fe0ef55; }}
.q-chip.bg-teal-1, .q-chip.bg-teal-2, .q-chip.bg-teal-3, .q-chip.bg-teal-4 {{ background:#12302c !important; color:#6fd0c0 !important; border-color:#6fd0c055; }}
.q-chip.bg-green-1, .q-chip.bg-green-2, .q-chip.bg-green-3, .q-chip.bg-green-4 {{ background:#12301e !important; color:#6fd08c !important; border-color:#6fd08c55; }}
.q-chip.bg-light-green-1, .q-chip.bg-light-green-2, .q-chip.bg-light-green-3, .q-chip.bg-light-green-4 {{ background:#1f3014 !important; color:#a6d870 !important; border-color:#a6d87055; }}
.q-chip.bg-lime-1, .q-chip.bg-lime-2, .q-chip.bg-lime-3, .q-chip.bg-lime-4 {{ background:#2a3012 !important; color:#d0e070 !important; border-color:#d0e07055; }}
.q-chip.bg-yellow-1, .q-chip.bg-yellow-2, .q-chip.bg-yellow-3, .q-chip.bg-yellow-4 {{ background:#36310f !important; color:#f0dc6a !important; border-color:#f0dc6a55; }}
.q-chip.bg-amber-1, .q-chip.bg-amber-2, .q-chip.bg-amber-3, .q-chip.bg-amber-4 {{ background:#3a2f12 !important; color:#f0c36a !important; border-color:#f0c36a55; }}
.q-chip.bg-orange-1, .q-chip.bg-orange-2, .q-chip.bg-orange-3, .q-chip.bg-orange-4 {{ background:#3a2812 !important; color:#f0a860 !important; border-color:#f0a86055; }}
.q-chip.bg-deep-orange-1, .q-chip.bg-deep-orange-2, .q-chip.bg-deep-orange-3, .q-chip.bg-deep-orange-4 {{ background:#3a2014 !important; color:#f08a60 !important; border-color:#f08a6055; }}
.q-chip.bg-brown-1, .q-chip.bg-brown-2, .q-chip.bg-brown-3, .q-chip.bg-brown-4 {{ background:#2f2419 !important; color:#d0a888 !important; border-color:#d0a88855; }}

/* tactical touches: corner-tick frames on cards, status footer */
.q-card {{ position:relative; }}
.q-card::after {{ content:''; position:absolute; inset:-1px; pointer-events:none; z-index:1; background-image:
  linear-gradient(var(--accent),var(--accent)), linear-gradient(var(--accent),var(--accent)),
  linear-gradient(var(--accent),var(--accent)), linear-gradient(var(--accent),var(--accent)),
  linear-gradient(var(--accent),var(--accent)), linear-gradient(var(--accent),var(--accent)),
  linear-gradient(var(--accent),var(--accent)), linear-gradient(var(--accent),var(--accent));
  background-repeat:no-repeat;
  background-size:8px 1px,1px 8px,8px 1px,1px 8px,8px 1px,1px 8px,8px 1px,1px 8px;
  background-position:left top,left top,right top,right top,left bottom,left bottom,right bottom,right bottom; }}
.q-card .q-card::after {{ display:none; }}
.q-card .text-lg, .q-card .text-base, .q-card .text-h6, .q-card .text-h5 {{ text-transform:uppercase; letter-spacing:.08em;
  font-size:12px !important; font-weight:600; }}
.tac-strip {{ position:fixed; left:0; right:0; bottom:0; height:20px; z-index:3000; display:flex; align-items:center;
  justify-content:space-between; padding:0 12px; background:#0a0c0a; border-top:1px solid var(--border);
  font:10px 'JetBrains Mono','SF Mono',Menlo,monospace; letter-spacing:.14em; color:var(--muted); }}
.tac-strip b {{ color:#7fa650; font-weight:600; }}
body {{ padding-bottom:20px; }}

/* scrollbars */
::-webkit-scrollbar {{ width:10px; height:10px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:#303730; border:2px solid var(--bg); }}
"""


def apply_theme() -> None:
    ui.dark_mode(True)
    ui.colors(primary=ACCENT, secondary="#6f786b", accent=ACCENT,
              positive="#7fa650", negative="#e5484d", warning="#e0a030",
              info=ACCENT, dark=RAISED)
    # NiceGUI loads Quasar inside a cascade layer ("@layer theme, base,
    # quasar, ..."). Unlayered rules beat it for normal declarations, but for
    # !important ones the order flips and a layered rule wins, so Quasar's
    # light bg-* tints (bg-color=..., chip colors) beat the unlayered theme.
    # The same rules inside the first layer ("theme") outrank Quasar's
    # !important ones too.
    ui.add_head_html(f"<style>{THEME_CSS}\n@layer theme {{\n{THEME_CSS}\n}}</style>")
    ui.add_body_html('<div class="tac-strip"><span>DPMG // GROUND STATION</span>'
                     '<span><b>&#9679;</b> SYSTEM NOMINAL</span><span>UNCLASSIFIED</span></div>')
