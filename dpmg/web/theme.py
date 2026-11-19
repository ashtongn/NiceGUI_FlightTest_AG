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

.q-chip.bg-grey-3, .q-chip[class*=bg-], .q-chip.bg-green-1, .q-chip.bg-grey-2, .q-chip.bg-grey-4 {{ background:var(--raised) !important; color:var(--text) !important; border:1px solid var(--border); }}
.q-chip.bg-green-1 {{ color:#6fd08c !important; }}

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
    ui.add_head_html(f"<style>{THEME_CSS}</style>")
    ui.add_body_html('<div class="tac-strip"><span>DPMG // GROUND STATION</span>'
                     '<span><b>&#9679;</b> SYSTEM NOMINAL</span><span>UNCLASSIFIED</span></div>')
