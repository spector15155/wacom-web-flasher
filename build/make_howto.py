"""Generate the "How the pen works" slides (web/howto/1.svg .. 8.svg: the stock cycle; m1.svg .. m6.svg: what each
firmware method changes) for the flasher.

Plain SVG, 800 x 450, dark theme. Content follows the measured stock scan loop: ~5 ms, ~200 per second:
locate (S1: wires listen, the closest hears the pen loudest, 3 neighbours give the position between wires),
power (P: transmit-only bursts recharge the pen), read data (S2: pressure and button bits as strong / weak replies),
then one report to the computer. Usage: python build/make_howto.py
"""
import math
import os

OUT = os.path.join(os.path.dirname(__file__), "..", "web", "howto")
W, H = 800, 450
BG, BAND, FG, MUTED, LINE = "#14171d", "#0e1116", "#e8ebf1", "#8b95a7", "#343a46"
BLUE, AMBER, GREEN, COPPER = "#4f8cff", "#f2b640", "#3ecf8e", "#c07a45"
FONT = "Inter, 'Segoe UI', system-ui, sans-serif"

WIRE_Y, WIRES = 332, [70 + 36 * k for k in range(15)]      # wire cross-sections in the tablet slab
TIP = (330, 297)                                             # pen tip (touching)
PEN_ROT = 17


def rot(x, y, a=PEN_ROT):
    t = math.radians(a)
    return x * math.cos(t) - y * math.sin(t), x * math.sin(t) + y * math.cos(t)


def at_pen(lx, ly, tip=TIP):
    dx, dy = rot(lx, ly)
    return tip[0] + dx, tip[1] + dy


def wrap(text, n=86):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return lines


def frame(body, caption, phase=None, tag=""):
    cap = wrap(caption)
    y0 = 418 - (len(cap) - 1) * 12
    caps = "".join(f'<tspan x="400" y="{y0 + 24 * i}">{c}</tspan>' for i, c in enumerate(cap))
    ring = phase_ring(phase) if phase else ""
    tagt = (f'<text x="712" y="49" text-anchor="end" font-size="11" letter-spacing="1.5" fill="{MUTED}">{tag}</text>'
            if tag else "")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="{FONT}">
<defs>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="5"/></filter>
  <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2"/></filter>
  <linearGradient id="slab" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#262b35"/><stop offset="1" stop-color="#1a1e25"/></linearGradient>
  <linearGradient id="penbody" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#5b6270"/><stop offset=".45" stop-color="#9aa1ad"/><stop offset="1" stop-color="#4a505c"/></linearGradient>
</defs>
<rect width="{W}" height="{H}" fill="{BG}"/>
{body}
{ring}{tagt}
<rect y="380" width="{W}" height="70" fill="{BAND}"/>
<text text-anchor="middle" font-size="17" fill="{FG}">{caps}</text>
</svg>
'''


def phase_ring(active):
    cx, cy, r = 752, 44, 20
    segs = (("locate", BLUE, -90, 18), ("power", AMBER, 18, 150), ("data", GREEN, 150, 270))
    out = []
    for name, col, a0, a1 in segs:
        a0r, a1r = math.radians(a0 + 4), math.radians(a1 - 4)
        x0, y0 = cx + r * math.cos(a0r), cy + r * math.sin(a0r)
        x1, y1 = cx + r * math.cos(a1r), cy + r * math.sin(a1r)
        large = 1 if (a1 - a0) > 180 else 0
        op = 1 if name == active else 0.18
        out.append(f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 {large} 1 {x1:.1f},{y1:.1f}" stroke="{col}" '
                   f'stroke-width="6" fill="none" stroke-linecap="round" opacity="{op}"/>')
    return "".join(out)


def slab(glow=None):
    """tablet cross-section: slab + a row of copper wires; glow = {wire index: (colour, strength 0..1)}"""
    s = [f'<rect x="40" y="300" width="580" height="62" rx="10" fill="url(#slab)" stroke="{LINE}"/>',
         f'<line x1="40" y1="300" x2="620" y2="300" stroke="#4a5160" stroke-width="2"/>',
         f'<text x="48" y="378" font-size="11" fill="{MUTED}" letter-spacing="1">TABLET (CROSS-SECTION) · WIRES</text>']
    for i, x in enumerate(WIRES):
        if glow and i in glow:
            col, k = glow[i]
            s.append(f'<circle cx="{x}" cy="{WIRE_Y}" r="{8 + 10 * k:.0f}" fill="{col}" opacity="{0.25 + 0.5 * k:.2f}" filter="url(#glow)"/>')
            s.append(f'<circle cx="{x}" cy="{WIRE_Y}" r="6" fill="{col}"/>')
        else:
            s.append(f'<circle cx="{x}" cy="{WIRE_Y}" r="6" fill="{COPPER}" opacity=".7"/>')
    return "".join(s)


def pen(tip=TIP, coil=None, button=None, cut=False):
    """slim stylus, tip at `tip`, tilted; coil = colour for a glowing coil; button = colour for the side button"""
    tx, ty = tip
    g = [f'<g transform="translate({tx},{ty}) rotate({PEN_ROT})">']
    g.append('<path d="M0,0 L-9,-34 L9,-34 Z" fill="#c9ced6"/>')
    op = ' opacity=".45"' if cut else ""
    g.append(f'<rect x="-12" y="-238" width="24" height="206" rx="10" fill="url(#penbody)"{op}/>')
    g.append('<rect x="-12" y="-238" width="24" height="206" rx="10" fill="none" stroke="#b9c0cb" stroke-width="1"/>')
    g.append(f'<rect x="11" y="-128" width="5" height="26" rx="2" fill="{button or "#3c424d"}"/>')
    if button:
        g.append(f'<rect x="11" y="-128" width="5" height="26" rx="2" fill="{button}" filter="url(#glow)"/>')
    if cut or coil:
        col = coil or COPPER
        g.append('<rect x="-3" y="-92" width="6" height="56" fill="#6e7480"/>')   # ferrite core
        for k in range(9):
            y = -88 + 6 * k
            g.append(f'<ellipse cx="0" cy="{y}" rx="9" ry="2.6" fill="none" stroke="{col}" stroke-width="2"/>')
        if coil:
            g.append(f'<rect x="-12" y="-94" width="24" height="60" rx="6" fill="{coil}" opacity=".35" filter="url(#glow)"/>')
    g.append("</g>")
    return "".join(g)


def label(x, y, text, col=MUTED, anchor="start", size=12):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{col}" text-anchor="{anchor}" letter-spacing=".5">{text}</text>'


def leader(x1, y1, x2, y2, col=MUTED):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{col}" stroke-width="1"/><circle cx="{x1}" cy="{y1}" r="2.5" fill="{col}"/>'


def f1():
    b = []
    # top view of the tablet with a peeled-back surface showing the wire grid
    b.append(f'<rect x="50" y="70" width="420" height="270" rx="16" fill="#1b1f27" stroke="{LINE}"/>')
    for k in range(13):
        x = 70 + 30 * k
        b.append(f'<line x1="{x}" y1="86" x2="{x}" y2="324" stroke="{COPPER}" stroke-width="1.6" opacity=".75"/>')
    for k in range(9):
        y = 90 + 28 * k
        b.append(f'<line x1="66" y1="{y}" x2="454" y2="{y}" stroke="{COPPER}" stroke-width="1.6" opacity=".55"/>')
    b.append(f'<path d="M50,70 L300,70 L50,250 Z" fill="#2b313c" opacity=".92"/>')    # surface layer, peeled
    b.append(f'<path d="M300,70 L50,250 L92,196 L262,86 Z" fill="#3a414e"/>')
    b.append(label(70, 110, "SURFACE", MUTED, size=11))
    b.append(leader(360, 200, 470, 360 - 150, COPPER) if False else "")
    b.append(label(260, 362, "a grid of thin copper wires under the surface", COPPER, "middle"))
    # pen cutaway, lying horizontally
    px, py = 520, 200
    b.append(f'<g transform="translate({px},{py})">')
    b.append('<path d="M0,0 L32,-10 L32,10 Z" fill="#c9ced6"/>')
    b.append('<rect x="30" y="-13" width="220" height="26" rx="12" fill="url(#penbody)" opacity=".35"/>')
    b.append('<rect x="30" y="-13" width="220" height="26" rx="12" fill="none" stroke="#b9c0cb"/>')
    b.append('<rect x="40" y="-3" width="64" height="6" fill="#6e7480"/>')
    for k in range(10):
        b.append(f'<ellipse cx="{44 + 6 * k}" cy="0" rx="2.6" ry="10" fill="none" stroke="{COPPER}" stroke-width="2"/>')
    b.append(f'<rect x="116" y="-8" width="3" height="16" fill="{FG}"/><rect x="123" y="-8" width="3" height="16" fill="{FG}"/>')
    b.append("</g>")
    b.append(leader(px + 72, py - 12, px + 72, py - 58) + label(px + 72, py - 64, "coil", FG, "middle"))
    b.append(leader(px + 121, py + 10, px + 121, py + 56) + label(px + 121, py + 72, "capacitor", FG, "middle"))
    # crossed-out battery
    bx, by = 650, 100
    b.append(f'<rect x="{bx}" y="{by}" width="44" height="22" rx="4" fill="none" stroke="{MUTED}" stroke-width="2"/>'
             f'<rect x="{bx + 44}" y="{by + 7}" width="4" height="8" fill="{MUTED}"/>'
             f'<line x1="{bx - 6}" y1="{by + 30}" x2="{bx + 54}" y2="{by - 8}" stroke="#e5484d" stroke-width="3"/>')
    b.append(label(bx + 22, by + 50, "no battery", FG, "middle"))
    b.append(label(655, 290, "the pen: a coil and a", MUTED, "middle") + label(655, 306, "capacitor that ring when", MUTED, "middle")
             + label(655, 322, "they receive a radio pulse", MUTED, "middle"))
    return frame("".join(b), "Inside the tablet: a grid of wires. Inside the pen: a coil and a capacitor, no battery.")


def field_lines(col, n_side=4, strength=1.0, dash=None):
    cx, cy = at_pen(0, -62)
    near = min(range(len(WIRES)), key=lambda i: abs(WIRES[i] - TIP[0]))
    out = []
    for d in range(-n_side, n_side + 1):
        i = near + d
        if not 0 <= i < len(WIRES):
            continue
        x = WIRES[i]
        op = strength * (1 - abs(d) / (n_side + 1.5))
        ctrl = (x + (cx - x) * 0.15, cy + 40)
        da = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<path d="M{x},{WIRE_Y - 8} Q{ctrl[0]:.0f},{ctrl[1]:.0f} {cx:.0f},{cy:.0f}" stroke="{col}" '
                   f'stroke-width="2" fill="none" opacity="{op:.2f}"{da}/>')
    return "".join(out)


def f2():
    b = [slab()]
    b.append(field_lines(AMBER, 5, 0.9))
    b.append(pen(coil=AMBER))
    for r in (26, 46, 66):
        b.append(f'<path d="M{TIP[0] - r},{TIP[1] + 4} A{r},{r} 0 0 0 {TIP[0] + r},{TIP[1] + 4}" stroke="{FG}" '
                 f'stroke-width="2" fill="none" stroke-dasharray="5 5" opacity="{0.9 - r / 110:.2f}"/>')
    b.append(label(120, 200, "1  the tablet sends a radio pulse", AMBER) + leader(250, 206, 290, 262, AMBER))
    b.append(label(470, 250, "2  the pen rings back", FG) + leader(468, 254, 372, 318, FG))
    b.append(label(655, 150, "like tapping a bell", MUTED, "middle") + label(655, 168, "and listening to it ring", MUTED, "middle"))
    return frame("".join(b), "The tablet powers the pen by radio; the pen rings back with a signal the wires can hear.")


def bars(x0, y0, vals, col, names=("left", "closest", "right")):
    out = []
    for k, v in enumerate(vals):
        x = x0 + 38 * k
        out.append(f'<rect x="{x}" y="{y0 - v}" width="26" height="{v}" rx="3" fill="{col}" opacity="{0.45 + 0.5 * v / max(vals):.2f}"/>')
        out.append(label(x + 13, y0 + 16, names[k], MUTED, "middle", 10))
    out.append(f'<line x1="{x0 - 8}" y1="{y0}" x2="{x0 + 110}" y2="{y0}" stroke="{LINE}"/>')
    return "".join(out)


HOVER_TIP = (TIP[0] - 5, TIP[1] - 20)


def f3():
    near = min(range(len(WIRES)), key=lambda i: abs(WIRES[i] - HOVER_TIP[0]))
    b = [slab({near: (BLUE, 1.0), near + 1: (BLUE, 0.55), near - 1: (BLUE, 0.3)})]
    for d, k in ((-1, 0.3), (0, 1.0), (1, 0.55)):
        x = WIRES[near + d]
        b.append(f'<line x1="{HOVER_TIP[0]}" y1="{HOVER_TIP[1]}" x2="{x}" y2="{WIRE_Y - 8}" stroke="{BLUE}" '
                 f'stroke-width="{1 + 2.5 * k:.1f}" opacity="{0.3 + 0.6 * k:.2f}" stroke-dasharray="3 4"/>')
    b.append(pen(tip=HOVER_TIP))
    b.append(bars(646, 250, (38, 104, 64), BLUE))
    b.append(label(700, 120, "received signal", FG, "middle") + label(700, 138, "per wire", MUTED, "middle"))
    return frame("".join(b), "Step 1 - locate: the wires near the pen listen. The one closest to the pen hears it loudest.",
                 "locate", "STEP 1 · LOCATE")


def f4():
    xs, vals, base = (230, 400, 570), (40, 110, 92), 300
    L, P, R = vals
    d = (R - L) / (2 * (2 * P - L - R))
    px = 400 + d * 170
    b = []
    b.append(f'<rect x="120" y="{base + 6}" width="560" height="40" rx="8" fill="url(#slab)" stroke="{LINE}"/>')
    for x, v, n in zip(xs, vals, ("left wire", "closest wire", "right wire")):
        b.append(f'<circle cx="{x}" cy="{base + 26}" r="9" fill="{BLUE}"/>')
        b.append(f'<rect x="{x - 22}" y="{base - v * 1.6:.0f}" width="44" height="{v * 1.6:.0f}" rx="4" fill="{BLUE}" '
                 f'opacity="{0.35 + 0.55 * v / P:.2f}"/>')
        b.append(label(x, base + 64, n, MUTED, "middle"))
    # smooth curve through the bar tops, peak marked
    pts = []
    for k in range(0, 61):
        t = -1.35 + 2.7 * k / 60
        a = (L + R - 2 * P) / 2
        bb = (R - L) / 2
        y = a * t * t + bb * t + P
        pts.append(f"{400 + t * 170:.1f},{base - y * 1.6:.1f}")
    b.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{FG}" stroke-width="2" stroke-dasharray="6 5" opacity=".8"/>')
    a2, b2 = (L + R - 2 * P) / 2, (R - L) / 2
    peak_y = base - (P - b2 * b2 / (4 * a2)) * 1.6
    b.append(f'<line x1="{px:.0f}" y1="{peak_y:.0f}" x2="{px:.0f}" y2="{base + 26}" stroke="{FG}" stroke-width="1.5" stroke-dasharray="2 3"/>')
    b.append(f'<circle cx="{px:.0f}" cy="{peak_y:.0f}" r="5" fill="{FG}"/>')
    b.append(f'<circle cx="{px:.0f}" cy="{base + 26}" r="12" fill="none" stroke="{FG}" stroke-width="2"/>'
             f'<line x1="{px - 18:.0f}" y1="{base + 26}" x2="{px + 18:.0f}" y2="{base + 26}" stroke="{FG}" stroke-width="2"/>')
    b.append(label(640, 96, "peak of the curve", FG, "middle") + label(640, 112, "= pen position", FG, "middle"))
    b.append(leader(px + 6, peak_y - 4, 600, 118, MUTED))
    return frame("".join(b), "Comparing the three signals gives the pen's exact position, between the wires.",
                 "locate", "STEP 1 · LOCATE")


def f5():
    b = [slab()]
    b.append(field_lines(AMBER, 6, 1.0))
    b.append(field_lines(AMBER, 3, 0.7, "2 6"))
    b.append(pen(coil=AMBER, cut=True))
    # energy gauge
    gx, gy = 690, 110
    b.append(f'<rect x="{gx}" y="{gy}" width="34" height="150" rx="6" fill="none" stroke="{MUTED}" stroke-width="2"/>')
    b.append(f'<rect x="{gx + 5}" y="{gy + 30}" width="24" height="115" rx="3" fill="{AMBER}"/>')
    b.append(label(gx + 17, gy - 12, "pen energy", FG, "middle"))
    b.append(label(gx + 17, gy + 172, "recharged", AMBER, "middle"))
    b.append(label(120, 190, "only sending, not listening", MUTED))
    return frame("".join(b), "Step 2 - power: the tablet sends a longer burst that recharges the pen.", "power",
                 "STEP 2 · POWER")


def f6():
    near = min(range(len(WIRES)), key=lambda i: abs(WIRES[i] - TIP[0]))
    b = [slab({near: (GREEN, 0.9)})]
    b.append(pen(button=GREEN))
    for r in (22, 40, 58):
        b.append(f'<path d="M{TIP[0] - r},{TIP[1] + 4} A{r},{r} 0 0 0 {TIP[0] + r},{TIP[1] + 4}" stroke="{GREEN}" '
                 f'stroke-width="2.5" fill="none" opacity="{1 - r / 80:.2f}"/>')
    bits = (1, 0, 1, 1, 0, 1)
    x0, y0 = 636, 230
    for k, v in enumerate(bits):
        x = x0 + 22 * k
        h = 70 if v else 20
        b.append(f'<rect x="{x}" y="{y0 - h}" width="14" height="{h}" rx="3" fill="{GREEN}" opacity="{1 if v else .45}"/>')
        b.append(label(x + 7, y0 + 20, str(v), FG, "middle", 14))
    b.append(label(700, 128, "strong = 1, weak = 0", MUTED, "middle"))
    b.append(label(700, 290, "pressure  ·  side buttons", FG, "middle"))
    bx, by = at_pen(16, -115)
    b.append(leader(bx + 4, by, 452, by - 30, MUTED) + label(458, by - 26, "side button", MUTED))
    b.append(leader(TIP[0] + 6, TIP[1] - 8, 420, 262, MUTED) + label(426, 266, "pressure on the tip", MUTED))
    return frame("".join(b), "Step 3 - read data: the pen sends how hard it is pressed and which button is held, "
                 "as strong and weak pulses.", "data", "STEP 3 · READ DATA")


def f7():
    cx, cy, r = 330, 178, 104
    segs = ((BLUE, -90, 18, "Locate", "where is the pen"), (AMBER, 18, 150, "Power", "recharge the pen"),
            (GREEN, 150, 270, "Read data", "pressure and buttons"))
    b = []
    for col, a0, a1, name, sub in segs:
        a0r, a1r = math.radians(a0 + 3), math.radians(a1 - 3)
        x0, y0 = cx + r * math.cos(a0r), cy + r * math.sin(a0r)
        x1, y1 = cx + r * math.cos(a1r), cy + r * math.sin(a1r)
        b.append(f'<path d="M{x0:.1f},{y0:.1f} A{r},{r} 0 0 1 {x1:.1f},{y1:.1f}" stroke="{col}" stroke-width="26" '
                 f'fill="none"/>')
        am = math.radians((a0 + a1) / 2)
        lx, ly = cx + (r + 44) * math.cos(am), cy + (r + 44) * math.sin(am) + (8 if math.sin(am) > 0.5 else 0)
        anc = "start" if math.cos(am) > 0.2 else "end" if math.cos(am) < -0.2 else "middle"
        b.append(label(lx, ly, name, col, anc, 16) + label(lx, ly + 18, sub, MUTED, anc, 12))
    # arrow head at the end of "read data" pointing back into "locate"
    a = math.radians(-93)
    hx, hy = cx + r * math.cos(a), cy + r * math.sin(a)
    b.append(f'<path d="M{hx - 14:.0f},{hy - 16:.0f} L{hx + 6:.0f},{hy:.0f} L{hx - 14:.0f},{hy + 16:.0f}" fill="none" '
             f'stroke="{FG}" stroke-width="3" stroke-linejoin="round"/>')
    b.append(label(cx, cy + 4, "~5 ms", FG, "middle", 34))
    b.append(label(cx, cy + 30, "one cycle", MUTED, "middle", 13))
    b.append(label(650, 170, "~200", FG, "middle", 40) + label(650, 196, "cycles per second", MUTED, "middle", 13))
    return frame("".join(b), "The three steps repeat in a loop of about 5 ms: roughly 200 times every second.")


def f8():
    b = []
    # tablet (top view, small)
    b.append(f'<rect x="60" y="110" width="250" height="170" rx="14" fill="#1b1f27" stroke="{LINE}"/>')
    b.append(f'<rect x="84" y="130" width="202" height="130" rx="6" fill="#20252e" stroke="#2c323d"/>')
    b.append(f'<path d="M120,230 C150,180 190,240 230,170" stroke="{FG}" stroke-width="2" fill="none" opacity=".5"/>')
    # cable
    b.append(f'<path d="M310,195 C380,195 400,300 470,300 S560,230 590,230" stroke="#5b6270" stroke-width="5" fill="none"/>')
    # packet
    b.append(f'<rect x="388" y="236" width="170" height="30" rx="15" fill="{BG}" stroke="{FG}" stroke-width="1.5"/>')
    b.append(label(473, 256, "x · y · pressure · buttons", FG, "middle", 12))
    # laptop
    b.append(f'<rect x="590" y="120" width="170" height="112" rx="6" fill="#0f1216" stroke="{LINE}" stroke-width="2"/>')
    b.append(f'<path d="M570,236 L780,236 L770,250 L580,250 Z" fill="#2b313c"/>')
    pts = [(612, 206), (630, 196), (650, 190), (670, 178), (690, 170), (710, 156), (728, 150)]
    b.append('<polyline points="' + " ".join(f"{x},{y}" for x, y in pts) + f'" stroke="{FG}" stroke-width="2" fill="none" opacity=".6"/>')
    for x, y in pts[:-1]:
        b.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{FG}" opacity=".6"/>')
    b.append(f'<circle cx="{pts[-1][0]}" cy="{pts[-1][1]}" r="6" fill="{BLUE}"/>')
    b.append(label(675, 272, "one new point per report", MUTED, "middle"))
    b.append(label(185, 312, "stock: one report per cycle", MUTED, "middle"))
    return frame("".join(b), "Each cycle ends with one report to the computer: position, pressure and buttons.")


# ---------------------------------------------------------------------------------------------------------------
# "What the firmwares change": position-over-time graphs (m1 .. m6). A small simulation of each method's timing and
# Wacom's moving average (results per ~5 ms cycle at the measured frame times, window N) draws the cursor staircase
# to scale against the pen's real movement.

CYC = 5.0                         # ms per scan cycle (measured ~4.97)
T0, T1 = 0.0, 30.0                # plotted time span (6 cycles)
GX0, GX1, GY0, GY1 = 70, 730, 96, 318


PEN_V0, PEN_V1 = 0.12, 0.88


def pen_pos(t):                   # steady movement (also before the plotted span), so the cursor's delay is constant
    return PEN_V0 + (PEN_V1 - PEN_V0) * t / T1


def lag_ms(series):
    """how far behind the pen the cursor is, averaged over the second half of the span (ms)"""
    acc, n = 0.0, 0
    for i in range(300):
        t = T1 / 2 + i * (T1 / 2) / 300
        o = [v for tt, v in series if tt <= t][-1]
        acc += t - (o - PEN_V0) * T1 / (PEN_V1 - PEN_V0)
        n += 1
    return acc / n


def gx(t):
    return GX0 + (t - T0) / (T1 - T0) * (GX1 - GX0)


def gy(v):
    return GY1 - v * (GY1 - GY0)


def simulate(result_offsets, window, s2=False):
    """results at k*CYC + offset; each result's value = the newest measurement at that time (S1 at k*CYC - 0.35,
    S2 at k*CYC + 3.1 when s2); output = mean of the last `window` result values. Returns [(t, out)], [(t, v, kind)]"""
    meas = []
    for k in range(-8, 9):
        meas.append((k * CYC - 0.35, "s1"))
        if s2:
            meas.append((k * CYC + 3.1, "s2"))
    meas.sort()
    res = []
    for k in range(-8, 7):
        for off in result_offsets:
            t = k * CYC + off
            avail = [(tm, kind) for tm, kind in meas if tm <= t - 0.05]
            tm, kind = avail[-1]
            res.append((t, pen_pos(tm)))
    res.sort()
    out = []
    for i, (t, _) in enumerate(res):
        vals = [v for _, v in res[max(0, i - window + 1):i + 1]]
        out.append((t, sum(vals) / len(vals)))
    shown = [(tm, pen_pos(tm), kind) for tm, kind in meas if T0 - 0.01 <= tm <= T1]
    return [(t, o) for t, o in out if T0 - CYC <= t <= T1], shown


def even(series, step, delay):
    """reports every `step` ms at the position of `series` (linear between results) `delay` ms earlier"""
    out = []
    t = T0 - CYC
    while t <= T1:
        q = t - delay
        pts = [(a, b) for a, b in series]
        for (ta, va), (tb, vb) in zip(pts, pts[1:]):
            if ta <= q <= tb:
                out.append((t, va + (vb - va) * (q - ta) / (tb - ta)))
                break
        t += step
    return out


def graph(series, meas, stats, name):
    b = []
    # axes, cycle bands
    for k in range(int(T1 / CYC)):
        x0, x1 = gx(k * CYC), gx((k + 1) * CYC)
        b.append(f'<rect x="{x0:.1f}" y="{GY0 - 10}" width="{x1 - x0:.1f}" height="{GY1 - GY0 + 20}" '
                 f'fill="{"#181c23" if k % 2 else "#1b2029"}"/>')
        b.append(label((x0 + x1) / 2, GY1 + 26, f"cycle {k + 1}", MUTED, "middle", 11))
    for ms in range(0, int(T1) + 1):
        x = gx(ms)
        h = 6 if ms % 5 else 10
        b.append(f'<line x1="{x:.1f}" y1="{GY1 + 10}" x2="{x:.1f}" y2="{GY1 + 10 + h}" stroke="{LINE}"/>')
    b.append(f'<line x1="{GX0}" y1="{GY1 + 10}" x2="{GX1}" y2="{GY1 + 10}" stroke="{LINE}"/>')
    b.append(label(GX1, GY1 + 44, "time (ms)", MUTED, "end", 11))
    b.append(label(GX0 - 8, GY0 + 4, "pen", MUTED, "end", 11) + label(GX0 - 8, GY0 + 18, "position", MUTED, "end", 11))
    # the pen's real movement
    pts = " ".join(f"{gx(T0 + i * (T1 - T0) / 200):.1f},{gy(pen_pos(T0 + i * (T1 - T0) / 200)):.1f}" for i in range(201))
    b.append(f'<polyline points="{pts}" fill="none" stroke="{MUTED}" stroke-width="2.5" opacity=".55"/>')
    # cursor staircase (clipped to the plotted span)
    before = [o for t, o in series if t <= T0]
    prev = before[-1] if before else series[0][1]
    st = [f"{gx(T0):.1f},{gy(prev):.1f}"]
    for t, o in series:
        if t <= T0:
            continue
        st.append(f"{gx(t):.1f},{gy(prev):.1f}")
        st.append(f"{gx(t):.1f},{gy(o):.1f}")
        prev = o
    st.append(f"{gx(T1):.1f},{gy(prev):.1f}")
    b.append(f'<polyline points="{" ".join(st)}" fill="none" stroke="#35c2ff" stroke-width="2.2"/>')
    dot_r = 2.6 if len([1 for t, _ in series if t >= T0]) < 60 else 1.6
    for t, o in series:
        if t >= T0:
            b.append(f'<circle cx="{gx(t):.1f}" cy="{gy(o):.1f}" r="{dot_r}" fill="#35c2ff"/>')
    # measurements
    for tm, v, kind in meas:
        col = FG if kind == "s1" else GREEN
        b.append(f'<circle cx="{gx(tm):.1f}" cy="{gy(v):.1f}" r="7" fill="none" stroke="{col}" stroke-width="2"/>')
    # legend + stats
    b.append(f'<line x1="72" y1="44" x2="96" y2="44" stroke="{MUTED}" stroke-width="2.5" opacity=".6"/>'
             + label(102, 48, "pen (real movement)", MUTED, size=12))
    b.append(f'<circle cx="250" cy="44" r="6" fill="none" stroke="{FG}" stroke-width="2"/>' + label(262, 48, "measurement", MUTED, size=12))
    if any(k == "s2" for _, _, k in meas):
        b.append(f'<circle cx="330" cy="66" r="6" fill="none" stroke="{GREEN}" stroke-width="2"/>'
                 + label(342, 70, "2nd measurement", MUTED, size=12))
    b.append(f'<line x1="72" y1="66" x2="96" y2="66" stroke="#35c2ff" stroke-width="2.2"/><circle cx="96" cy="66" r="2.6" fill="#35c2ff"/>'
             + label(102, 70, "cursor (each dot = one report)", MUTED, size=12))
    b.append(label(730, 40, name, FG, "end", 15))
    b.append(label(730, 60, stats, MUTED, "end", 12))
    return "".join(b)


V245 = (0.0, 2.65, 3.5, 4.3)


def m1():
    ser, meas = simulate((0.0,), 4)
    return frame(graph(ser, meas, "~200 measurements/s · ~200 reports/s", "Stock"),
                 "Stock: one measurement, one report per cycle. The cursor jumps and trails the pen "
                 "(Wacom averages the last 4 reports).")


def m2():
    ser, meas = simulate(V245, 4)
    return frame(graph(ser, meas, "~200 measurements/s · ~730 reports/s", "More Wacom runs: v1.65, v2.45"),
                 "More Wacom runs: the same measurements, but Wacom's calculation runs 3-4 times per cycle, "
                 "so the cursor moves in smaller steps and stays closer to the pen.")


def m3():
    base, meas = simulate(V245, 4)
    ser = even(base, 1.0, 3.25)
    return frame(graph(ser, meas, "~200 measurements/s · 1000 or 2000 reports/s", "Even output: v2.99, v3.29, v3.28"),
                 "Even output: a report exactly every 1 ms (0.5 ms on v3.28), placed on the path between "
                 "measurements. Never ahead of the pen: nothing is guessed.")


def m4():
    near = min(range(len(WIRES)), key=lambda i: abs(WIRES[i] - TIP[0]))
    b = [slab({near: (GREEN, 0.9), near - 1: (BLUE, 0.55), near + 1: (BLUE, 0.75)})]
    b.append(pen())
    for r in (22, 40):
        b.append(f'<path d="M{TIP[0] - r},{TIP[1] + 4} A{r},{r} 0 0 0 {TIP[0] + r},{TIP[1] + 4}" stroke="{GREEN}" '
                 f'stroke-width="2.5" fill="none" opacity="{1 - r / 70:.2f}"/>')
    for d in (-1, 1):
        x = WIRES[near + d]
        b.append(f'<line x1="{TIP[0]}" y1="{TIP[1] + 2}" x2="{x}" y2="{WIRE_Y - 8}" stroke="{BLUE}" stroke-width="2" '
                 f'stroke-dasharray="3 4" opacity=".8"/>')
    b.append(label(95, 170, "stock step 3: only the wire under", MUTED) + label(95, 186, "the pen listens (to read the data)", MUTED))
    b.append(label(95, 226, "two measurements: its neighbours", BLUE) + label(95, 242, "listen too, during the same pulses", BLUE))
    b.append(label(655, 130, "data still read", GREEN, "middle", 13) + label(655, 150, "(pressure, buttons)", MUTED, "middle"))
    b.append(label(655, 200, "+ a second position", BLUE, "middle", 13) + label(655, 220, "in every cycle", MUTED, "middle"))
    b.append(label(655, 250, "(~400 per second", MUTED, "middle") + label(655, 266, "instead of ~200)", MUTED, "middle"))
    return frame("".join(b), "Two measurements (v3.62, v3.78): while the pen sends its data, the neighbouring wires "
                 "listen too, so every cycle gives a second real position.", "data", "STEP 3 · READ DATA + LOCATE")


def m5():
    ser, meas = simulate((0.0, 3.5), 4, s2=True)
    return frame(graph(ser, meas, "~400 measurements/s · ~400 reports/s", "Two measurements: v3.62"),
                 "v3.62: twice the real measurements, one report for each (~400 per second, in even steps). "
                 "Nothing is added between them.")


def m6():
    ser, meas = simulate((0.0, 0.6, 1.2, 1.8, 2.65, 3.5, 4.3), 7, s2=True)
    return frame(graph(ser, meas, "~400 measurements/s · ~1500 reports/s", "Two measurements + more runs: v3.78"),
                 "v3.78: twice the measurements, and Wacom's calculation runs about 7 times per cycle: "
                 "~1500 reports per second in fine steps.")


FRAMES = (f1, f2, f3, f4, f5, f6, f7, f8)
METHOD_FRAMES = (m1, m2, m3, m4, m5, m6)

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for k, fn in enumerate(FRAMES, 1):
        path = os.path.join(OUT, f"{k}.svg")
        open(path, "w", encoding="utf-8").write(fn())
        print("wrote", path)
    for k, fn in enumerate(METHOD_FRAMES, 1):
        path = os.path.join(OUT, f"m{k}.svg")
        open(path, "w", encoding="utf-8").write(fn())
        print("wrote", path)
