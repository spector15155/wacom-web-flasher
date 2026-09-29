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

#ifndef WORK
#error "WORK (frame work area) must be defined"
#endif

#define IMG      ((volatile uint8_t *)0x2001D358)
#define ST       ((volatile struct state *)0x2003F780)
#define LOG      ((volatile uint8_t *)0x20034000)
#define NLOG     256
#define ESZ      0x80
#define MAGIC    0x53325835u
#define ONE      1000
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
    /* an axis is moved only if its neighbours belong to this peak and both S1 passes were centred on it
     * (edge windows are clamped: the TX peak is not at window entry 4 there) */
    uint8_t mask = 0;
    if (s->nb[0] == px && s->nb[3] == px && s->cen[0] && s->cen[1])
        mask |= 1;
    if (s->nb[4] == py && s->nb[7] == py && s->cen[2] && s->cen[3])
        mask |= 2;
    uint8_t xl = s->nb[1], xr = s->nb[2], yl = s->nb[5], yr = s->nb[6];

    if (hdr == 0x8F && all_peak(7, px, py)) {
        if (mask & 1) { IMG[0x17] = xl; IMG[0x18] = xr; IMG[0x19] = xl; }
        if (mask & 2) { IMG[0x21] = yl; IMG[0x22] = yr; IMG[0x23] = yl; }
        s->coils[0] = xl; s->coils[1] = px; s->coils[2] = xr;
        s->coils[3] = yl; s->coils[4] = py; s->coils[5] = yr;
        s->pmask = mask;
        if (mask) { s->na++; tag = 1 | mask << 2; } else { s->skip++; }
    } else if (hdr == 0x8E) {
        if (all_peak(6, px, py)) {
            mask &= s->pmask;
            s->pmask = 0;
            if (mask & 2) { IMG[0x1E] = yr; IMG[0x1F] = yl; IMG[0x20] = yr; }
            if (mask & 1) { IMG[0x1A] = xr; IMG[0x1B] = xl; IMG[0x1C] = xr; }
            if (mask) { s->nbb++; tag = 2 | mask << 2; } else { s->skip++; }
        } else {
            /* S1: slot 4 = peak -> slot 3 = window offset 3 (pass a) or 5 (pass b, slot 3 == slot 5) */
            if (IMG[0x1B] == px) {
                uint8_t a = IMG[0x1A], b = IMG[0x1C];
                if (a != px) {
                    if (a == b) { s->nb[3] = px; s->nb[2] = a; s->cen[1] = 1; }
                    else        { s->nb[0] = px; s->nb[1] = a; s->cen[0] = 1; }
                }
            } else {
                s->cen[0] = s->cen[1] = 0;
            }
            if (IMG[0x22] == py) {
                uint8_t a = IMG[0x21], b = IMG[0x23];
                if (a != py) {
                    if (a == b) { s->nb[7] = py; s->nb[6] = a; s->cen[3] = 1; }
                    else        { s->nb[4] = py; s->nb[5] = a; s->cen[2] = 1; }
                }
            } else {
                s->cen[2] = s->cen[3] = 0;
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
static void scale_entry(volatile uint8_t *e, int a)
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
static int amp_of(volatile uint8_t *e, int pass, int ch, int i)
{
    int v = rd16(e + 0xC + ((pass * 2 + ch) * 6 + i) * 2);
    int p = e[0x3C + (pass * 2 + ch) * 6 + i];          /* phase / 2 */
    if (p >= 35 && p <= 58)
        v = v * 1000 / 1048;
    return v;
}

/* neighbour sample valid: the peak channel of the same burst read a '1' */
static void nb_sample(volatile uint8_t *e, int pass, int ch, int i, struct acc *a)
{
    if (rd16(e + 0xC + ((pass * 2 + (ch ^ 1)) * 6 + i) * 2) > ONE)
        add(a, amp_of(e, pass, ch, i));
}

static void pk_sample(volatile uint8_t *e, int pass, int ch, int i, struct acc *a)
{
    if (rd16(e + 0xC + ((pass * 2 + ch) * 6 + i) * 2) > ONE)
        add(a, amp_of(e, pass, ch, i));
}

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

static void inject(volatile uint8_t *e, uint8_t mask)
{
    volatile struct state *s = ST;
    int L[2], P[2], R[2];
    uint8_t ok = 0;
    e[0xA] = 0;
    e[0x70] = FRAME[0x0C];
    e[0x71] = FRAME[0xB6];
    for (int ch = 0; ch < 2; ch++) {
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 30;
        for (int k = 0; k < 3; k++)
            wr16(e + (ch == 0 ? 0x64 : 0x6A) + 2 * k, rd16(ent + 10 * k));
        if (!(mask & (1 << ch)) || !lpr(e, ch, &L[ch], &P[ch], &R[ch]))
            continue;
        wr16(e + (ch == 0 ? 0x72 : 0x78), L[ch]);
        wr16(e + (ch == 0 ? 0x74 : 0x7A), P[ch]);
        wr16(e + (ch == 0 ? 0x76 : 0x7C), R[ch]);
        /* first window: Wacom's edge extrapolation (clamped windows are excluded by the centring check) */
        int s1p = rd16(ent + 10);
        if (FRAME[ch == 0 ? 0x0C : 0xB6] == 0 || s1p < 200)
            continue;
        /* the frame's entry 4 must be the coil S2 listened around: Wacom may have re-centred its window between
         * the S1 scan in the frame and this S2 (v3.10 log: P2/P1 far from 1 in >10 % of loops -> sideways jumps).
         * Same coil within ~2 ms reads the same amplitude (matched loops: ratio medians agree to 0.005). */
        if (P[ch] * 5 < s1p * 4 || P[ch] * 4 > s1p * 5)
            continue;
        ok |= 1 << ch;
    }
    /* both axes or neither: a new X with the old Y (or the reverse) puts the point off the stroke */
    if (ok != 3) {
        if (ok & 1) s->gate_y++; else s->gate_x++;
        return;
    }
#ifndef NOINJECT
    for (int ch = 0; ch < 2; ch++) {
        volatile uint8_t *ent = FRAME + (ch == 0 ? 0x11 : 0xBB) + 30;
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
void s2x_event(uint8_t *r)
{
    volatile struct state *s = ST;
    if (s->magic != MAGIC)
        return;
    if (s->idx >= NLOG)
        s->idx = 0;
    if (STEPB == 24) {
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
        int x_moved = (pass == 0) == (i < 3);
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
