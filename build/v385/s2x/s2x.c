/*
 * S2 neighbour listening, split per pass (v3.04+, built by tools/make_s2c.py).
 *
 * S2 pass a (hdr 0x8F, 7 bursts) and pass b (0x8E, 6 bursts) receive the pen's data bits on the peak coil with
 * both channels (identical bits). Each pass here moves the RX coil (L1) of ONE axis per half:
 *   pass a  bursts 0-2: X = L,R,L (Y on peak)    bursts 3-5: Y = L,R,L (X on peak)
 *   pass b  bursts 0-2: Y = R,L,R (X on peak)    bursts 3-5: X = R,L,R (Y on peak)
 * so X and Y get the same mean measurement time. TX lists (L2) are never touched.
 * On each result the peak axis's entries are copied over the moved ones before Wacom's handlers run (exact bits).
 * At the pass b result the frame work area's profile entries 3/4/5 are scaled to the S2 left/peak/right means
 * (interpolation only: skipped per axis unless P >= L and P >= R), so the frame pushed after step 29 carries a
 * second real measurement for Wacom's own position calc.
 *
 * All state lives at fixed SRAM addresses (no .data / .bss).
 */
#include <stdint.h>

#ifdef LEAN
#define LEAN_ON 1
#else
#define LEAN_ON 0
#endif

#ifndef WORK
#error "WORK (frame work area) must be defined"
#endif

#define IMG      ((volatile uint8_t *)0x2001D358)
#define ST       ((volatile struct state *)0x2003F780)
#define LOG      ((volatile uint8_t *)0x20034000)
#ifdef LEAN
#define NLOG     1                       /* one scratch entry: no analysis log */
#else
#define NLOG     256
#endif
#define ESZ      0x80
#define MAGIC    0x53325835u
#define ONE      1000
#ifndef S2MIN
#define S2MIN    2000
#endif
#define CALC_X   (*(volatile int32_t *)(0x20010E0C + 0xFA4))
#define CALC_Y   (*(volatile int32_t *)(0x20010E0C + 0x10D8))
#define STEPB    (*(volatile uint8_t *)STEP_ADDR)
#define FRAME    ((volatile uint8_t *)WORK)

struct state {
    uint8_t nb[8];          /* X peak (S1 a), XL, XR, X peak (S1 b), Y same */
    uint32_t ev;            /* events */
    uint8_t tag[4];         /* program tag ring, indexed by event count */
    uint32_t na, nbb, skip; /* pass a / pass b rewrites, skipped (no neighbours) */
    uint8_t coils[6];       /* XL XP XR YL YP YR of the current loop */
    uint8_t half, mask;     /* pass a result seen / axes moved in pass a (bit0 X, bit1 Y) */
    uint32_t idx, seq;
    uint32_t inj_x, inj_y, gate_x, gate_y;
    uint8_t cen[4];         /* S1 program centred (slot 4 = TX peak): X pass a, X pass b, Y pass a, Y pass b */
    uint8_t pmask, pad2[3]; /* axes moved in the last pass a program */
    uint8_t saved[40];      /* RESTORE: S1 profile entries 3 / 5 (X, Y) before the injection */
    uint8_t has_saved, pad3[3];
    uint8_t ek[2], ekl[2];  /* EDGE2: peak window offset per axis from S1 pass a (4 = centred), latched at S2 */
    uint8_t rk[2], pad5[2]; /* RESTORE: peak offset of the saved entries */
    uint32_t n_boost, n_power;  /* PBOOST: power steps lengthened / seen */
    uint16_t last_tx, last_period; uint8_t last_gain, pad6[3];
    uint32_t magic;
};

/* log entry (ESZ 0x80): +0 seq, +4 coils[6], +0xA inject flags, +0xC amp[pass][ch][6] s16 (ch 0 X, 1 Y),
 * +0x3C phase/2 [pass][ch][6], +0x54 cx, +0x58 cy (per-scan, raw), +0x5C / +0x60 same at the next step-24 event,
 * +0x64 S1 X entries 3,4,5 before inject, +0x6A Y, +0x70 window X, Y, +0x72 X L,P,R, +0x78 Y L,P,R */

static inline int16_t rd16(volatile const uint8_t *p) { return (int16_t)(p[0] | p[1] << 8); }
static inline void wr16(volatile uint8_t *p, int v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

static int all_peak(int n, uint8_t px, uint8_t py)
{
    for (int i = 0; i < n; i++)
        if (IMG[0x17 + i] != px || IMG[0x1E + i] != py)
            return 0;
    return 1;
}

void s2x_step(void)
{
    volatile struct state *s = ST;
    if (s->magic != MAGIC) {
        uint8_t *p = (uint8_t *)ST;
        for (unsigned i = 0; i < sizeof(struct state); i++)
            p[i] = 0;
        s->magic = MAGIC;
    }
    uint8_t tag = 0, hdr = IMG[3], px = IMG[0x4C], py = IMG[0x53];
#ifdef PBOOST
    /* power steps (program 0x8B / 0x8C: transmit-only bursts that charge the pen): the sensor's automatic gain
     * (+0x0E, ~141 with the tip down, 170-205 in hover) shows how far the pen is. The further away, the longer the
     * transmit burst (u16 +0x06, up to +PBOOST %), so the pen is charged more; the step period (u16 +0x46 = bursts x
     * transmit + 8) grows by the same time per burst. Measuring steps are never touched. */
    if (hdr == 0x8B || hdr == 0x8C) {
        uint16_t tx = (uint16_t)(IMG[6] | IMG[7] << 8), per = (uint16_t)(IMG[0x46] | IMG[0x47] << 8);
        uint8_t g = IMG[0x0E];
        s->n_power++;
        s->last_gain = g;
        if (tx >= 200 && per > tx && g > 160) {
            int w = g >= 210 ? 50 : g - 160;                /* 0..50 */
            int add = tx * PBOOST * w / (100 * 50);
            int reps = (per - 8 + tx / 2) / tx;
            if (add > 0 && reps >= 1 && reps <= 8) {
                int ntx = tx + add, nper = per + reps * add;
                if (ntx < 0xFFFF && nper < 0xFFFF) {
                    IMG[6] = (uint8_t)ntx; IMG[7] = (uint8_t)(ntx >> 8);
                    IMG[0x46] = (uint8_t)nper; IMG[0x47] = (uint8_t)(nper >> 8);
                    s->n_boost++;
                    s->last_tx = (uint16_t)ntx;
                    s->last_period = (uint16_t)nper;
                }
            }
        }
    }
#endif
#ifdef PBOOST_ONLY
    s->tag[s->ev & 3] = 0;
    return;                                         /* no S2 rewriting in this build */
#endif
    /* an axis is moved only if its neighbours belong to this peak and both S1 passes were centred on it
     * (edge windows are clamped: the TX peak is not at window entry 4 there) */
    uint8_t mask = 0;
    if (s->nb[0] == px && s->nb[3] == px && s->cen[0] && s->cen[1])
        mask |= 1;
    if (s->nb[4] == py && s->nb[7] == py && s->cen[2] && s->cen[3])
        mask |= 2;
    uint8_t xl = s->nb[1], xr = s->nb[2], yl = s->nb[5], yr = s->nb[6];

#ifdef S2GATE
    /* top / left edge window (Wacom clamps the coil window, first window starts at 0): the injection is always
     * rejected there, so leave S2 stock (v3.67 log: ~190 rewrites/s near the edges, 0 used) */
#ifdef EDGE2
    /* first window: usable when the peak sits at offset 1..4 of it (4 = centred on coil 4 of the border) */
    if ((FRAME[0x0C] == 0 && !(s->ek[0] >= 1 && s->ek[0] <= 4)) ||
        (FRAME[0xB6] == 0 && !(s->ek[1] >= 1 && s->ek[1] <= 4)))
        mask = 0;
#else
    if (FRAME[0x0C] == 0 || FRAME[0xB6] == 0)
        mask = 0;
#endif
#endif
#ifdef S2NORM
    /* ratio-normalised layout (both axes needed): bursts 0 and 5 both on the peak (X/Y gain ratio), the others one
     * axis on a neighbour while the other stays on its peak, so dividing the two channels of a burst cancels the
     * pen's own strength swings (data bits, phase bits, energy):
     *   pass a  X: P L R P P P   Y: P P P L R P
     *   pass b  X: L P R P P P   Y: P P P L P R
     * Pass b bursts 2, 3 and 5 carry status bits that are '1' in 98-100 % of loops (pass a: pressure bits, 70-78 %),
     * so every neighbour has a second chance on a near-certain burst; the gain ratio comes from bursts 0 / 5 (pass a)
     * and 1 / 4 (pass b), falling back to the other pass. (v3.47 layout pass b X P P P R L P / Y P R L P P P lost
     * X's left sample in ~1 / 3 of loops while drawing.) */
    if (mask != 3)
        mask = 0;
#endif
    if (hdr == 0x8F && all_peak(7, px, py)) {
#ifdef S2NORM
        if (mask) { IMG[0x18] = xl; IMG[0x19] = xr; IMG[0x21] = yl; IMG[0x22] = yr; }
#else
        if (mask & 1) { IMG[0x17] = xl; IMG[0x18] = xr; IMG[0x19] = xl; }
        if (mask & 2) { IMG[0x21] = yl; IMG[0x22] = yr; IMG[0x23] = yl; }
#endif
        s->ekl[0] = s->ek[0]; s->ekl[1] = s->ek[1];
        s->coils[0] = xl; s->coils[1] = px; s->coils[2] = xr;
        s->coils[3] = yl; s->coils[4] = py; s->coils[5] = yr;
        s->pmask = mask;
        if (mask) { s->na++; tag = 1 | mask << 2; } else { s->skip++; }
    } else if (hdr == 0x8E) {
        if (all_peak(6, px, py)) {
            mask &= s->pmask;
            s->pmask = 0;
#ifdef S2NORM
#ifdef LAYOUT1
            if (mask) { IMG[0x1F] = yr; IMG[0x20] = yl; IMG[0x1A] = xr; IMG[0x1B] = xl; }
#else
            if (mask) { IMG[0x17] = xl; IMG[0x19] = xr; IMG[0x21] = yl; IMG[0x23] = yr; }
#endif
#else
            if (mask & 2) { IMG[0x1E] = yr; IMG[0x1F] = yl; IMG[0x20] = yr; }
            if (mask & 1) { IMG[0x1A] = xr; IMG[0x1B] = xl; IMG[0x1C] = xr; }
#endif
            if (mask) { s->nbb++; tag = 2 | mask << 2; } else { s->skip++; }
        } else {
            /* S1: slot 4 = peak -> slot 3 = window offset 3 (pass a) or 5 (pass b, slot 3 == slot 5) */
            if (IMG[0x1B] == px) {
                uint8_t a = IMG[0x1A], b = IMG[0x1C];
                if (a != px) {
                    if (a == b) { s->nb[3] = px; s->nb[2] = a; s->cen[1] = 1; }
                    else        { s->nb[0] = px; s->nb[1] = a; s->cen[0] = 1; }
                }
                s->ek[0] = 4;
            }
#ifdef EDGE2
            else if (FRAME[0x0C] == 0 && IMG[0x1A] != IMG[0x1C]) {
                /* pass a of the clamped left window (slots = offsets 0,1,2,3,4,9): peak at offset 1..3, both
                 * neighbours are in this list; pass b (offsets 8..4) has none of them and keeps these */
                int k = IMG[0x18] == px ? 1 : IMG[0x19] == px ? 2 : IMG[0x1A] == px ? 3 : 0;
                if (k) {
                    s->nb[0] = s->nb[3] = px;
                    s->nb[1] = IMG[0x17 + k - 1];
                    s->nb[2] = IMG[0x17 + k + 1];
                    s->cen[0] = s->cen[1] = 1;
                } else {
                    s->cen[0] = s->cen[1] = 0;
                }
                s->ek[0] = (uint8_t)k;
            } else if (FRAME[0x0C] == 0 && s->ek[0] >= 1 && s->ek[0] <= 3) {
                /* pass b of an edge loop */
            }
#endif
            else {
                s->cen[0] = s->cen[1] = 0;
                s->ek[0] = 0;
            }
            if (IMG[0x22] == py) {
                uint8_t a = IMG[0x21], b = IMG[0x23];
                if (a != py) {
                    if (a == b) { s->nb[7] = py; s->nb[6] = a; s->cen[3] = 1; }
                    else        { s->nb[4] = py; s->nb[5] = a; s->cen[2] = 1; }
                }
                s->ek[1] = 4;
            }
#ifdef EDGE2
            else if (FRAME[0xB6] == 0 && IMG[0x21] != IMG[0x23]) {
                int k = IMG[0x1F] == py ? 1 : IMG[0x20] == py ? 2 : IMG[0x21] == py ? 3 : 0;
                if (k) {
                    s->nb[4] = s->nb[7] = py;
                    s->nb[5] = IMG[0x1E + k - 1];
                    s->nb[6] = IMG[0x1E + k + 1];
                    s->cen[2] = s->cen[3] = 1;
                } else {
                    s->cen[2] = s->cen[3] = 0;
                }
                s->ek[1] = (uint8_t)k;
            } else if (FRAME[0xB6] == 0 && s->ek[1] >= 1 && s->ek[1] <= 3) {
            }
#endif
            else {
                s->cen[2] = s->cen[3] = 0;
                s->ek[1] = 0;
            }
        }
    }
    s->tag[s->ev & 3] = tag;
}

static void copy_entry(volatile uint8_t *dst, volatile const uint8_t *src)
{
    for (int k = 0; k < 10; k++)
        dst[k] = src[k];
}

/* entry amplitude -> a (scales amp, I, Q, amp copy) */
__attribute__((unused)) static void scale_entry(volatile uint8_t *e, int a)
{
    int old = rd16(e);
    if (old < 200)
        return;
    for (int k = 0; k < 8; k += 2)
        wr16(e + k, rd16(e + k) * a / old);
}

struct acc { int sum, n; };

static void add(struct acc *a, int v) { a->sum += v; a->n++; }

/* amplitude of burst i on channel ch; the pen's phase-shifted ('90 deg') data bits read ~4.8 % stronger on
 * every coil alike (v3.06 log: 2447 vs 2335, neighbour/peak ratio unchanged), so they are scaled back */
__attribute__((unused)) static int amp_of(volatile uint8_t *e, int pass, int ch, int i)
{
    int v = rd16(e + 0xC + ((pass * 2 + ch) * 6 + i) * 2);
    int p = e[0x3C + (pass * 2 + ch) * 6 + i];          /* phase / 2 */
    if (p >= 35 && p <= 58)
        v = v * 1000 / 1048;
    return v;
}

/* neighbour sample valid: the peak channel of the same burst read a '1' */
__attribute__((unused)) static void nb_sample(volatile uint8_t *e, int pass, int ch, int i, struct acc *a)
{
    if (rd16(e + 0xC + ((pass * 2 + (ch ^ 1)) * 6 + i) * 2) > ONE)
        add(a, amp_of(e, pass, ch, i));
}

__attribute__((unused)) static void pk_sample(volatile uint8_t *e, int pass, int ch, int i, struct acc *a)
{
    if (rd16(e + 0xC + ((pass * 2 + ch) * 6 + i) * 2) > ONE)
        add(a, amp_of(e, pass, ch, i));
}

#ifdef S2NORM
static int nm_amp(volatile uint8_t *e, int p, int ch, int i)
{
    return rd16(e + 0xC + ((p * 2 + ch) * 6 + i) * 2);
}

/* S2NORM layout (see s2x_step). Every neighbour reading is divided by the other axis's peak reading of the SAME
 * burst (cancels the pen's strength swings: data bits, phase bits, energy) and corrected by the X/Y peak gain
 * ratio of that pass (bursts 0 and 5, both axes on the peak). v3.42 at rest: p90 noise ~6 counts vs 12-13 raw.
 * Returns L / R as ratio x the axis's mean peak amplitude and P = that amplitude, so inject() can keep using
 * L * s1p / P and its coil-match gate on P. */
static int lpr(volatile uint8_t *e, int ch, int *L, int *P, int *R)
{
#ifdef LAYOUT1
    static const uint8_t lb[2][2][2] = {{{0, 1}, {1, 4}}, {{0, 3}, {1, 2}}};   /* [ch][k] = pass, burst of L */
    static const uint8_t rb[2][2][2] = {{{0, 2}, {1, 3}}, {{0, 4}, {1, 1}}};
    static const uint8_t pk[2][2][4] = {{{0, 3, 4, 5}, {0, 1, 2, 5}},          /* [ch][pass] bursts on the peak */
                                        {{0, 1, 2, 5}, {0, 3, 4, 5}}};
    static const uint8_t gb[2][2] = {{0, 5}, {0, 5}};                        /* [pass] bursts with both on peak */
#else
    static const uint8_t lb[2][2][2] = {{{0, 1}, {1, 0}}, {{0, 3}, {1, 3}}};   /* [ch][k] = pass, burst of L */
    static const uint8_t rb[2][2][2] = {{{0, 2}, {1, 2}}, {{0, 4}, {1, 5}}};
    static const uint8_t pk[2][2][4] = {{{0, 3, 4, 5}, {1, 3, 4, 5}},          /* [ch][pass] bursts on the peak */
                                        {{0, 1, 2, 5}, {0, 1, 2, 4}}};
    static const uint8_t gb[2][2] = {{0, 5}, {1, 4}};                        /* [pass] bursts with both on peak */
#endif
    int g[2];
    for (int p = 0; p < 2; p++) {                   /* X_P / Y_P gain ratio x1024 */
        int sum = 0, n = 0;
        for (int k = 0; k < 2; k++) {
            int i = gb[p][k], x = nm_amp(e, p, 0, i), y = nm_amp(e, p, 1, i);
            if (x > ONE && y > ONE) {
                sum += x * 1024 / y;
                n++;
            }
        }
        g[p] = n ? sum / n : 0;
    }
    if (!g[0] && !g[1])
        return 0;
    if (!g[0]) g[0] = g[1];                         /* same two coils ~1 ms apart: the ratio carries over */
    if (!g[1]) g[1] = g[0];
    int lr = 0, ln = 0, rr = 0, rn = 0, ps = 0, pn = 0;
    for (int k = 0; k < 2; k++) {
        for (int side = 0; side < 2; side++) {
            const uint8_t *b = side ? rb[ch][k] : lb[ch][k];
            int own = nm_amp(e, b[0], ch, b[1]), oth = nm_amp(e, b[0], ch ^ 1, b[1]);
            if (oth <= ONE)
                continue;
            int r = own * 1024 / oth;
            r = ch == 0 ? r * 1024 / g[b[0]] : r * g[b[0]] / 1024;
            if (side) { rr += r; rn++; } else { lr += r; ln++; }
        }
    }
    for (int p = 0; p < 2; p++)
        for (int k = 0; k < 4; k++) {
            int v = nm_amp(e, p, ch, pk[ch][p][k]);
            if (v > ONE) {
                ps += v;
                pn++;
            }
        }
    if (!ln || !rn || !pn)
        return 0;
    int pa = ps / pn;
    *P = pa;
    *L = lr / ln * pa / 1024;
    *R = rr / rn * pa / 1024;
    return 1;
}
#else
/* ch 0 = X, 1 = Y. Pass a: X nb bursts 0-2 (L R L), Y nb 3-5 (L R L); pass b: Y nb 0-2 (R L R), X nb 3-5 (R L R) */
static int lpr(volatile uint8_t *e, int ch, int *L, int *P, int *R)
{
    struct acc l = {0, 0}, r = {0, 0}, p = {0, 0};
    int a0 = ch == 0 ? 0 : 3, b0 = ch == 0 ? 3 : 0;
    nb_sample(e, 0, ch, a0 + 0, &l);
    nb_sample(e, 0, ch, a0 + 1, &r);
    nb_sample(e, 0, ch, a0 + 2, &l);
    nb_sample(e, 1, ch, b0 + 0, &r);
    nb_sample(e, 1, ch, b0 + 1, &l);
    nb_sample(e, 1, ch, b0 + 2, &r);
    for (int i = 0; i < 3; i++) {
        pk_sample(e, 0, ch, b0 + i, &p);
        pk_sample(e, 1, ch, a0 + i, &p);
    }
    if (!l.n || !r.n || !p.n)
        return 0;
    *L = l.sum / l.n;
    *P = p.sum / p.n;
    *R = r.sum / r.n;
    return 1;
}

#endif

static void inject(volatile uint8_t *e, uint8_t mask)
{
    volatile struct state *s = ST;
    int L[2], P[2], R[2];
    uint8_t ok = 0;
    e[0xA] = 0;
    e[0x70] = FRAME[0x0C];
    e[0x71] = FRAME[0xB6];
    int kk[2] = {4, 4};
#ifdef EDGE2
    for (int ch = 0; ch < 2; ch++)
        if (s->ekl[ch] >= 1 && s->ekl[ch] <= 3)
            kk[ch] = s->ekl[ch];
#endif
    for (int ch = 0; ch < 2; ch++) {
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 10 * (kk[ch] - 1);   /* entries k-1, k, k+1 */
        for (int k = 0; k < 3; k++)
            wr16(e + (ch == 0 ? 0x64 : 0x6A) + 2 * k, rd16(ent + 10 * k));
        if (!(mask & (1 << ch)) || !lpr(e, ch, &L[ch], &P[ch], &R[ch]))
            continue;
        wr16(e + (ch == 0 ? 0x72 : 0x78), L[ch]);
        wr16(e + (ch == 0 ? 0x74 : 0x7A), P[ch]);
        wr16(e + (ch == 0 ? 0x76 : 0x7C), R[ch]);
        /* first window: Wacom's edge extrapolation (clamped windows are excluded by the centring check) */
        int s1p = rd16(ent + 10);
        uint8_t w0 = FRAME[ch == 0 ? 0x0C : 0xB6];
        /* an edge offset (k 1..3) only in the clamped first window; centred (k 4) in any window with EDGE2 */
#ifdef EDGE2
        if ((kk[ch] != 4 && w0 != 0) || s1p < 200)
            continue;
#else
        if (w0 == 0 || s1p < 200)
            continue;
#endif
        /* the frame's entry 4 must be the coil S2 listened around: Wacom may have re-centred its window between
         * the S1 scan in the frame and this S2 (v3.10 log: P2/P1 far from 1 in >10 % of loops -> sideways jumps).
         * Same coil within ~2 ms reads the same amplitude (matched loops: ratio medians agree to 0.005). */
#ifdef S2GATE
        /* v3.67: S2 only with a strong signal (offline replay: peak < 2000 -> 3-5x the error, noisier than S1) and a
         * tighter same-coil match (+-10 %): S2 error rms -30 %, 70 % of loops still injected (75 % before) */
        if (P[ch] < S2MIN || P[ch] * 11 < s1p * 10 || P[ch] * 10 > s1p * 11)
            continue;
#else
        if (P[ch] * 5 < s1p * 4 || P[ch] * 4 > s1p * 5)
            continue;
#endif
        ok |= 1 << ch;
    }
    /* both axes or neither: a new X with the old Y (or the reverse) puts the point off the stroke */
    if (ok != 3) {
        if (ok & 1) s->gate_y++; else s->gate_x++;
        return;
    }
#ifndef NOINJECT
#ifdef RESTORE
    for (int ch = 0; ch < 2; ch++) {
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 10 * (kk[ch] - 1);
        for (int j = 0; j < 10; j++) {
            s->saved[ch * 20 + j] = ent[j];
            s->saved[ch * 20 + 10 + j] = ent[20 + j];
        }
        s->rk[ch] = (uint8_t)kk[ch];
    }
    s->has_saved = 1;
#endif
    for (int ch = 0; ch < 2; ch++) {
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 10 * (kk[ch] - 1);
        /* keep S1's peak amplitude, give entries 3 / 5 the S2 left / right ratios */
        int s1p = rd16(ent + 10);
        scale_entry(ent, L[ch] * s1p / P[ch]);
        scale_entry(ent + 20, R[ch] * s1p / P[ch]);
    }
#endif
    e[0xA] = 3;
    s->inj_x++;
    s->inj_y++;
}

/* r = unpacked result struct: X entry i at r + 1 + 10 i, Y entry at + 0x46 */
#ifdef RESTORE
void s2x_restore(void);
#endif

void s2x_event(uint8_t *r)
{
    volatile struct state *s = ST;
    if (s->magic != MAGIC)
        return;
#ifdef RESTORE_EV
    /* builds without an output hook: the injected frame was pushed after the previous event returned, so put S1's
     * own entries back now, before this event's handler builds the next frame (v2.45's step-23 frame reuses them) */
    if (s->has_saved)
        s2x_restore();
#endif
    if (s->idx >= NLOG)
        s->idx = 0;
    if (!LEAN_ON && STEPB == 24) {
        volatile uint8_t *pe = LOG + ((s->idx + NLOG - 1) % NLOG) * ESZ;
        *(volatile int32_t *)(pe + 0x5C) = CALC_X;
        *(volatile int32_t *)(pe + 0x60) = CALC_Y;
    }
    uint32_t ev = ++s->ev;
    uint8_t tag = s->tag[(ev - 3) & 3];
    s->tag[(ev - 3) & 3] = 0;
    if (!tag)
        return;
    int pass = (tag & 3) - 1;
    uint8_t mask = tag >> 2;
    volatile uint8_t *e = LOG + s->idx * ESZ;
    for (int i = 0; i < 6; i++) {
        uint8_t *x = r + 1 + 10 * i, *y = x + 0x46;
        wr16(e + 0xC + ((pass * 2 + 0) * 6 + i) * 2, rd16(x));
        wr16(e + 0xC + ((pass * 2 + 1) * 6 + i) * 2, rd16(y));
        e[0x3C + (pass * 2 + 0) * 6 + i] = (uint8_t)((x[8] | x[9] << 8) >> 1);
        e[0x3C + (pass * 2 + 1) * 6 + i] = (uint8_t)((y[8] | y[9] << 8) >> 1);
        /* pass a: X moved in 0-2, Y moved in 3-5; pass b: Y moved in 0-2, X moved in 3-5 */
#ifdef S2NORM
        /* moved axis per burst: pass a X in 1-2, Y in 3-4; pass b X in 0 and 2, Y in 3 and 5 */
        int x_moved, y_moved;
        if (pass == 0) {
            x_moved = i == 1 || i == 2;
            y_moved = i == 3 || i == 4;
        } else {
#ifdef LAYOUT1
            x_moved = i == 3 || i == 4;         /* v3.45 layout: pass b X P P P R L P / Y P R L P P P */
            y_moved = i == 1 || i == 2;
#else
            x_moved = i == 0 || i == 2;
            y_moved = i == 3 || i == 5;
#endif
        }
        if (!x_moved && !y_moved)
            continue;                           /* both axes on the peak: nothing to copy */
#else
        int x_moved = (pass == 0) == (i < 3);
#endif
        if (x_moved) {
            if (mask & 1)
                copy_entry(x, y);
        } else if (mask & 2) {
            copy_entry(y, x);
        }
    }
    if (pass == 0) {
        for (int k = 0; k < 6; k++)
            e[4 + k] = s->coils[k];
        s->half = 1;
        s->mask = mask;
        return;
    }
    if (!s->half)
        return;
    s->half = 0;
    *(volatile int32_t *)(e + 0x54) = CALC_X;
    *(volatile int32_t *)(e + 0x58) = CALC_Y;
    inject(e, mask & s->mask);
    uint32_t q = ++s->seq;
    *(volatile uint32_t *)e = q;
    s->idx = (s->idx + 1) % NLOG;
}

#ifdef RESTORE
/* called from the ring push hook once the step-29 frame is copied to the stage: put S1's own entries 3 / 5 back, so
 * frames pushed later from the same work area (next loop's step 23: new S1 pass a + this loop's pass b) carry S1
 * data only, as in v2.45, not the S2 injection of an older pen position */
void s2x_restore(void)
{
    volatile struct state *s = ST;
    if (s->magic != MAGIC || !s->has_saved)
        return;
    s->has_saved = 0;
    for (int ch = 0; ch < 2; ch++) {
        int k = s->rk[ch] >= 1 && s->rk[ch] <= 4 ? s->rk[ch] : 4;
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 10 * (k - 1);
        for (int j = 0; j < 10; j++) {
            ent[j] = s->saved[ch * 20 + j];
            ent[20 + j] = s->saved[ch * 20 + 10 + j];
        }
    }
}
#endif
