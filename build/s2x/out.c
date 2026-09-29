/*
 * Output engine: one pen report per 1 ms HID tick on an even timeline (v3.17).
 *
 * Positions come ONLY from the in-range pen records Wacom itself builds (its calc and report builder run at the
 * stock v2.45 cadence: pressure, buttons, proximity, filtering untouched). Each record is paired with the time its
 * frame was handed to the calc (ring push stamp, carried through a small queue filled at the calc's mail put and
 * matched by the result's X/Y), so the history holds Wacom's own positions with their measurement times.
 *
 * Every HID tick: the in-range record (Wacom's, or a repeat of the last one if no result arrived) gets X/Y = the
 * history interpolated at now - DELAY (never beyond the newest point: no prediction). Records that leave range and
 * vendor reports pass untouched. The history restarts after STALE without an in-range record.
 *
 * Extra filtering, only where Wacom's own output is poor:
 *   far hover (level 1): median of the last 3 positions (single wild values from the coarse search)
 *   top / left edge strip (coil window 0, Wacom extrapolates): per-axis average over up to 8 points, by speed
 *
 * Hooks (make_s2c.py): out_push (ring push entry), out_pop (calc ring pop call), out_put (calc mail put call),
 * out_hid (HID pen routine call). v3.16 took positions from every calc result instead, including results Wacom
 * does not report in range (0 / garbage at the range limit): cursor flicks to the top-left corner.
 */
#include <stdint.h>

#ifndef DELAY_US
#define DELAY_US 4000
#endif
#ifndef EDGE_IN
#define EDGE_IN  5000                    /* full edge smoothing below this X / Y (report counts, ~5 um) */
#endif
#ifndef EDGE_OUT
#define EDGE_OUT 8000                    /* none above */
#endif
#define CYC_US   96                      /* SYSCLK 96 MHz */
#define DELAY    (DELAY_US * CYC_US)
#define STALE    (12000 * CYC_US)        /* no in-range record for 12 ms: restart the history */
#define FRESH    (20000 * CYC_US)        /* a queued stamp older than this is not trusted */

#define DWT_CYC  (*(volatile uint32_t *)0xE0001004)
#define DWT_CTRL (*(volatile uint32_t *)0xE0001000)
#define DEMCR    (*(volatile uint32_t *)0xE000EDFC)

#define CALC     ((volatile uint8_t *)0x20010E0C)
#define RING_W   (*(volatile uint16_t *)(0x2000F8E8 + 0x151E))
#define RING_R   (*(volatile uint16_t *)(0x2000F8E8 + 0x1520))
#define CUR      ((volatile uint8_t *)CUR_ADDR)
#define OUT      ((volatile struct out *)0x2003C000)   /* 0x2003C000-0x2003CFFF: no references in the stock image */
#define OMAGIC   0x4F55543Eu
#define NSD      5                       /* shadow delays graded: 0, 0.5, 1.0, 1.5, 2.0 ms */
#define NSE      8
#ifndef PREDICT
#define PREDICT  0
#endif
#define PRED_MAX   (6000 * CYC_US)          /* look-ahead limit past the newest position */
#define NPE      16                      /* predictions kept for grading */
#define STEPB    (*(volatile uint8_t *)STEP_ADDR)
#define NH       8                       /* interpolation history */
#define NR       8                       /* raw positions for the filters */
#define NQ       8                       /* calc result stamps waiting for their record */

struct pt { uint32_t t; int32_t x, y; };
struct qe { uint32_t t; int32_t x, y; };

struct out {
    uint32_t magic;
    uint32_t stamp[3];                  /* ring slot -> push time */
    uint32_t cur_stamp;                 /* push time of the frame the calc is processing */
    uint32_t qw, qr;                    /* stamp queue: written by the calc task, read by the HID task */
    struct qe q[NQ];
    struct pt h[NH];                    /* HID task only */
    uint32_t hn;                        /* points in the history (newest = h[(hn - 1) % NH]) */
    uint32_t last_rec;                  /* time of the last in-range record */
    int32_t rx[NR], ry[NR];             /* raw positions (filters) */
    uint32_t ewin[2];
    uint8_t rec[27];                    /* last in-range record (fills) */
    uint8_t rec_ok, pad[4];
    /* counters */
    uint32_t n_ticks, n_rec, n_fill, n_leave, n_hold, n_restart;
    uint32_t n_qmatch, n_qmiss, n_spike, n_edge, n_zero;
    /* 2 reports per tick (appended: counters above keep their offsets) */
    uint32_t last_tick;
    uint32_t n_double, n_pack2, n_pack1;
    uint8_t pbuf[64];                   /* USB packet with two pen reports */
    uint32_t due[2];                    /* paced loops: HID, USB */
    uint8_t skips[2], pad2[2];
    uint32_t n_catchup[2];
    uint32_t age_hist[16];              /* age of the newest point at each in-range tick, 0.5 ms bins */
    uint8_t lvl, pad3[3];               /* tracking level of the newest point */
    uint32_t n_pred;                    /* positions extended past the newest point */
    /* loop-phase timeline (v3.23) */
    uint32_t anchor;                    /* push time of this loop's step-24 frame */
    uint32_t period;                    /* loop period (EMA of step-24 push intervals) */
    uint32_t n_realstamp;               /* frames stamped with real time (no loop phase known) */
    /* prediction grading: predicted positions, checked once Wacom's real position for that time arrives */
    struct pe { uint32_t t; int32_t x, y; uint32_t valid; } pe[NPE];
    uint32_t pe_w;
    uint32_t perr_n, perr_sum, perr_hist[8]; /* |dx|+|dy| counts: <10 <20 <40 <80 <160 <320 <640 >=640 */
    uint32_t last_stamp;                /* stamp of the previous frame (fallback timeline) */
    /* shadow grading: the same look-ahead evaluated for several delays on the same motion, once per tick */
    struct sd { struct pe pe[NSE]; uint32_t w, n, sum, hist[8]; } sd[NSD];
};

#ifndef DOUBLE
#define DOUBLE 1
#endif

typedef uint32_t (*fn0)(void);

static void init(volatile struct out *o)
{
    uint8_t *p = (uint8_t *)o;
    for (unsigned i = 0; i < sizeof(struct out); i++)
        p[i] = 0;
    DEMCR |= 1u << 24;
    DWT_CTRL |= 1u;
    o->magic = OMAGIC;
}

/* ring push entry: stamp the slot the frame goes into */
void out_push(void)
{
    volatile struct out *o = OUT;
    if (o->magic != OMAGIC)
        init(o);
    uint16_t w = RING_W;
    if (w >= 3)
        return;
    /* Wacom's calc moves its output one equal step per frame, but the frames of a ~4.85 ms loop reach it at
     * ~0 / 2.65 / 3.5 / 4.3 ms (steps 24, 28, 29, 23). Stamping them with those real times makes the path look ~3x
     * faster in the bunched part (look-ahead overshoot, e.g. on circles). Stamp them at 0, 1/4, 1/2, 3/4 of the
     * measured loop period after this loop's step-24 frame instead. */
    uint32_t now = DWT_CYC;
    uint8_t st = STEPB;
    int ph = st == 24 ? 0 : st == 28 ? 1 : st == 29 ? 2 : st == 23 ? 3 : -1;
    if (ph == 0) {
        uint32_t d = now - o->anchor;
        if (d > 3500 * CYC_US && d < 7000 * CYC_US)
            o->period = o->period ? (o->period * 7 + d) / 8 : d;
        o->anchor = now;
    }
    uint32_t T = o->period ? o->period : 4850 * CYC_US;
    uint32_t st_ = now;
    if (ph >= 0 && o->anchor && (uint32_t)(now - o->anchor) < T + 1000 * CYC_US) {
        st_ = o->anchor + (uint32_t)ph * (T / 4);
    } else {
        /* frame from a step outside the normal loop (Wacom's alternate scan path, mostly in hover): continue the
         * even timeline a quarter period after the previous frame, never later than now (v3.25: raw arrival
         * times here, ~9 % of frames, made the direction estimate lopsided again) */
        uint32_t nx = o->last_stamp + T / 4;
        if (o->last_stamp && (int32_t)(now - nx) >= 0 && (uint32_t)(now - nx) < T)
            st_ = nx;
        o->n_realstamp++;
    }
    o->stamp[w] = st_;
    o->last_stamp = st_;
}

/* calc ring pop: the slot at the read index is the frame being processed */
void out_pop(void)
{
    volatile struct out *o = OUT;
    uint16_t r = RING_R;
    if (o->magic == OMAGIC && r < 3)
        o->cur_stamp = o->stamp[r];
}

/* calc mail put: queue (stamp, output X/Y) of this result for the HID task */
void out_put(void)
{
    volatile struct out *o = OUT;
    if (o->magic != OMAGIC)
        return;
    uint32_t w = o->qw;
    volatile struct qe *e = &o->q[w % NQ];
    e->t = o->cur_stamp;
    e->x = *(volatile int32_t *)(CALC + 0x1C);
    e->y = *(volatile int32_t *)(CALC + 0x54);
    o->qw = w + 1;
}

/* stamp of the result whose output is (x, y): oldest queued entry with that X/Y; entries before it are dropped */
static uint32_t match_stamp(volatile struct out *o, int32_t x, int32_t y, uint32_t now)
{
    uint32_t w = o->qw, r = o->qr;
    if (w - r > NQ)
        r = w - NQ;
    for (uint32_t k = r; k != w; k++) {
        volatile struct qe *e = &o->q[k % NQ];
        if (e->x == x && e->y == y) {
            o->qr = k + 1;
            uint32_t t = e->t;
            if ((uint32_t)(now - t) < FRESH) {
                o->n_qmatch++;
                return t;
            }
            break;
        }
    }
    o->n_qmiss++;
    return now - 1500 * CYC_US;                 /* typical frame -> record time */
}

static int32_t med3(int32_t a, int32_t b, int32_t c)
{
    return a > b ? (b > c ? b : (a > c ? c : a)) : (a > c ? a : (b > c ? c : b));
}

/* per-axis edge smoothing (top / left strip): see the header */
static int32_t edge_smooth(volatile struct out *o, int ax, volatile int32_t *ring, uint32_t n, int32_t v)
{
    int32_t w = v <= EDGE_IN ? 256 : v >= EDGE_OUT ? 0 : (EDGE_OUT - v) * 256 / (EDGE_OUT - EDGE_IN);
    int32_t d = n >= 4 ? v - ring[(n - 4) % NR] : 0;
    if (d < 0)
        d = -d;
    uint32_t want = d < 40 ? 8 : d < 100 ? 6 : d < 200 ? 4 : d < 400 ? 2 : 1;
    uint32_t cur = o->ewin[ax];
    if (cur < 1)
        cur = 1;
    cur = want > cur ? cur + 1 : want < cur ? cur - 1 : cur;
    o->ewin[ax] = cur;
    if (!w || cur < 2)
        return v;
    uint32_t m = cur > n + 1 ? n + 1 : cur;
    int32_t sum = 0;
    for (uint32_t k = 0; k < m; k++)
        sum += ring[(n - k) % NR];
    o->n_edge++;
    return v + (sum / (int32_t)m - v) * w / 256;
}

/* add Wacom's record position (x, y) measured at t */
static void add_point(volatile struct out *o, uint32_t t, int32_t x, int32_t y, uint8_t lvl)
{
    uint32_t n = o->hn;                         /* raw ring index = history count */
    o->rx[n % NR] = x;
    o->ry[n % NR] = y;
    if (lvl == 1 && n >= 2) {
        int32_t mx = med3(o->rx[(n - 2) % NR], o->rx[(n - 1) % NR], x);
        int32_t my = med3(o->ry[(n - 2) % NR], o->ry[(n - 1) % NR], y);
        if (mx != x || my != y)
            o->n_spike++;
        x = mx;
        y = my;
    }
    x = edge_smooth(o, 0, o->rx, n, x);
    y = edge_smooth(o, 1, o->ry, n, y);
    if (n) {                                    /* keep the history's times increasing */
        uint32_t tp = o->h[(n - 1) % NH].t;
        if ((int32_t)(t - tp) <= 0)
            t = tp + 1;
    }
    volatile struct pt *p = &o->h[n % NH];
    p->t = t;
    p->x = x;
    p->y = y;
    o->lvl = lvl;
    o->hn = n + 1;
#ifdef POINTLOG
    /* diagnostic: every history point (loop-phase time, x, y, level) into a RAM ring for offline tuning */
    volatile uint32_t *lg = (volatile uint32_t *)0x20034000;
    uint32_t k = lg[0] % 2048;
    lg[1 + 3 * k] = t;
    lg[2 + 3 * k] = (uint32_t)x;
    lg[3 + 3 * k] = (uint32_t)y | (uint32_t)lvl << 24 | (o->hn == 1 ? 1u << 31 : 0);
    lg[0] = lg[0] + 1;
#endif
}

/* position at time t: interpolated between the two history points around t. Past the newest point: with
 * PREDICT and pred set, continue along the direction of the last loop of Wacom's path (<= PRED_MAX ahead, not in
 * far hover), else hold the newest point. Returns 1 if it predicted. */
static int interp_core(volatile struct out *o, uint32_t t, int32_t *x, int32_t *y, int pred)
{
    uint32_t n = o->hn;
    volatile struct pt *nw = &o->h[(n - 1) % NH];
    uint32_t lim = n < NH ? n : NH;
    if ((int32_t)(t - nw->t) >= 0) {
        *x = nw->x;
        *y = nw->y;
#if PREDICT
        uint32_t h = t - nw->t;
        uint32_t T = o->period ? o->period : 4850 * CYC_US;
        if (pred && o->lvl >= 2 && h <= PRED_MAX) {
            /* direction from a point about one loop older: every phase of the loop is covered once */
            for (uint32_t k = 2; k <= lim; k++) {
                volatile struct pt *m = &o->h[(n - k) % NH];
                uint32_t span = nw->t - m->t;
                if (span >= T * 9 / 10 && span < T * 5 / 2) {
                    int32_t dt = (int32_t)span >> 8, f = (int32_t)h >> 8;
                    *x = nw->x + (nw->x - m->x) * f / dt;
                    *y = nw->y + (nw->y - m->y) * f / dt;
                    if (pred == 1)
                        o->n_pred++;
                    return 1;
                }
            }
        }
#endif
        if (pred == 1)
            o->n_hold++;
        return 0;
    }
    for (uint32_t k = 2; k <= lim; k++) {
        volatile struct pt *a = &o->h[(n - k) % NH], *b = &o->h[(n - k + 1) % NH];
        if ((int32_t)(t - a->t) >= 0) {
            /* 2.7 us units keep dx * f inside 32 bits (gaps up to 50 ms, dx up to 100000) */
            int32_t dt = (int32_t)(b->t - a->t) >> 8, f = (int32_t)(t - a->t) >> 8;
            if (dt <= 0) {
                *x = b->x;
                *y = b->y;
            } else {
                *x = a->x + (b->x - a->x) * f / dt;
                *y = a->y + (b->y - a->y) * f / dt;
            }
            return 0;
        }
    }
    volatile struct pt *old = &o->h[(n - lim) % NH];
    *x = old->x;
    *y = old->y;
    return 0;
}

static void interp(volatile struct out *o, uint32_t t, int32_t *x, int32_t *y)
{
    if (interp_core(o, t, x, y, 1)) {           /* remember the prediction for grading */
        volatile struct pe *e = &o->pe[o->pe_w++ % NPE];
        e->t = t;
        e->x = *x;
        e->y = *y;
        e->valid = 1;
    }
}

/* grade predictions whose time is now covered by Wacom's real positions */
static void grade(volatile struct out *o)
{
    uint32_t n = o->hn, lim = n < NH ? n : NH;
    uint32_t newest = o->h[(n - 1) % NH].t, oldest = o->h[(n - lim) % NH].t;
    for (int k = 0; k < NPE; k++) {
        volatile struct pe *e = &o->pe[k];
        if (!e->valid || (int32_t)(e->t - newest) > 0)
            continue;
        e->valid = 0;
        if ((int32_t)(e->t - oldest) < 0)
            continue;
        int32_t x, y;
        interp_core(o, e->t, &x, &y, 0);
        uint32_t d = (uint32_t)((e->x > x ? e->x - x : x - e->x) + (e->y > y ? e->y - y : y - e->y));
        o->perr_n++;
        o->perr_sum += d;
        uint32_t b = d < 10 ? 0 : d < 20 ? 1 : d < 40 ? 2 : d < 80 ? 3 : d < 160 ? 4 : d < 320 ? 5 : d < 640 ? 6 : 7;
        o->perr_hist[b]++;
    }
    for (int i = 0; i < NSD; i++) {
        volatile struct sd *g = &o->sd[i];
        for (int k = 0; k < NSE; k++) {
            volatile struct pe *e = &g->pe[k];
            if (!e->valid || (int32_t)(e->t - newest) > 0)
                continue;
            e->valid = 0;
            if ((int32_t)(e->t - oldest) < 0)
                continue;
            int32_t x, y;
            interp_core(o, e->t, &x, &y, 0);
            uint32_t d = (uint32_t)((e->x > x ? e->x - x : x - e->x) + (e->y > y ? e->y - y : y - e->y));
            g->n++;
            g->sum += d;
            g->hist[d < 10 ? 0 : d < 20 ? 1 : d < 40 ? 2 : d < 80 ? 3 : d < 160 ? 4 : d < 320 ? 5 : d < 640 ? 6 : 7]++;
        }
    }
}

/* once per in-range tick: what each shadow delay would have sent now (exact where Wacom's path already
 * covers the target: those grade as 0) */
static void shadow(volatile struct out *o, uint32_t now)
{
    for (int i = 0; i < NSD; i++) {
        volatile struct sd *g = &o->sd[i];
        int32_t x, y;
        uint32_t t = now - (uint32_t)i * 500 * CYC_US;
        interp_core(o, t, &x, &y, 2);
        volatile struct pe *e = &g->pe[g->w++ % NSE];
        e->t = t;
        e->x = x;
        e->y = y;
        e->valid = 1;
    }
}

static void put_xy(volatile uint8_t *r, int32_t x, int32_t y)
{
    if (x < 0) x = 0;
    if (y < 0) y = 0;
    r[2] = (uint8_t)x; r[3] = (uint8_t)(x >> 8); r[4] = (uint8_t)(x >> 16);
    r[5] = (uint8_t)y; r[6] = (uint8_t)(y >> 8); r[7] = (uint8_t)(y >> 16);
}

/* second record of a tick: copy of the first at the later timeline position */
static uint32_t make_double(volatile struct out *o, volatile uint8_t *rec, uint32_t now, uint32_t half,
                            uint32_t n)
{
    int32_t x, y;
    interp(o, now - DELAY - half, &x, &y);
    put_xy(rec, x, y);
    for (int j = 0; j < 27; j++)
        rec[27 + j] = rec[j];
    interp(o, now - DELAY, &x, &y);
    put_xy(rec + 27, x, y);
    o->n_double++;
    return (n & ~0xFu) | 2;
}

/* HID tick: wraps the stock pen routine; returns its record count / flags (low nibble = records).
 * With DOUBLE, a tick with one in-range pen record (Wacom's or a fill) sends two: at now - DELAY - half the tick
 * interval and at now - DELAY, so the reports stay evenly spaced even when the HID loop runs late. */
uint32_t out_hid(void)
{
    volatile struct out *o = OUT;
    uint32_t n = ((fn0)(PEN_FN | 1))();
    volatile uint8_t *rec = (volatile uint8_t *)RECORDS;
    if (o->magic != OMAGIC || (n & 0x80))
        return n;                               /* not initialised yet / vendor report tick */
    uint32_t now = DWT_CYC;
    uint32_t dtick = now - o->last_tick;
    o->last_tick = now;
    if (dtick < 500 * CYC_US) dtick = 500 * CYC_US;
    if (dtick > 2000 * CYC_US) dtick = 2000 * CYC_US;
    uint32_t half = dtick / 2;
    o->n_ticks++;
    if (o->hn && (int32_t)(now - o->last_rec) > STALE) {
        o->hn = 0;                              /* no in-range record for 12 ms: new history */
        for (int k = 0; k < NPE; k++)
            o->pe[k].valid = 0;
        for (int i = 0; i < NSD; i++)
            for (int k = 0; k < NSE; k++)
                o->sd[i].pe[k].valid = 0;
        o->rec_ok = 0;
        o->n_restart++;
    }
    uint32_t cnt = n & 0xF;
    int32_t x, y;
    int single_in = 0;
    if (o->hn && o->rec_ok)
        shadow(o, now);
    if (o->hn && o->rec_ok) {                   /* headroom: how old is the newest point right now */
        uint32_t age = (now - o->h[(o->hn - 1) % NH].t) / (500 * CYC_US);
        o->age_hist[age > 15 ? 15 : age]++;
    }
    for (uint32_t k = 0; k < cnt && k < 3; k++) {
        volatile uint8_t *r = rec + 27 * k;
        if (r[0] != 0x10)
            continue;
        if (!(r[1] & 0x20)) {                   /* leaving range: stock record; no fills until back */
            o->rec_ok = 0;
            o->n_leave++;
            continue;
        }
        int32_t sx = r[2] | r[3] << 8 | r[4] << 16, sy = r[5] | r[6] << 8 | r[7] << 16;
        if (sx == 0 && sy == 0) {               /* never a real position */
            o->n_zero++;
        } else {
            add_point(o, match_stamp(o, sx, sy, now), sx, sy, CUR[0x794]);
            o->n_rec++;
            grade(o);
        }
        o->last_rec = now;
        if (o->hn) {
            interp(o, now - DELAY, &x, &y);
            put_xy(r, x, y);
            single_in = cnt == 1;
        }
        for (int j = 0; j < 27; j++)
            o->rec[j] = r[j];
        o->rec_ok = 1;
    }
    if (single_in && DOUBLE)
        return make_double(o, rec, now, half, n);
    if (!cnt && o->rec_ok && o->hn) {
        /* no calc result this tick: repeat the last in-range record's state at the new position */
        for (int j = 0; j < 27; j++)
            rec[j] = o->rec[j];
        o->n_fill++;
        if (DOUBLE)
            return make_double(o, rec, now, half, n);
        interp(o, now - DELAY, &x, &y);
        put_xy(rec, x, y);
        return (n & ~0xFu) | 1;
    }
    return n;
}

/* USB pen report send (usbif task, endpoint free): if a second pen report is queued, send both in one packet
 * (64 B endpoint, the host HID class splits a transfer holding two reports). Replaces send(buf, 27). */
struct evt { uint32_t status, value, def; };
typedef void (*get_t)(struct evt *, uint32_t, uint32_t);
typedef uint32_t (*free_t)(uint32_t, uint32_t);
typedef void (*send_t)(volatile uint8_t *, uint32_t);

void out_usb_pen(uint8_t *buf, uint32_t len)
{
    volatile struct out *o = OUT;
    volatile uint32_t *q = (volatile uint32_t *)USBQ;
    if (o->magic == OMAGIC && len == 27 && buf[0] == 0x10 && ((volatile uint8_t *)q)[4] == 1) {
        struct evt e;
        ((get_t)(USB_GET | 1))(&e, q[0], 0);
        if (e.status == 0x20) {
            volatile uint8_t *m = (volatile uint8_t *)e.value;
            for (int j = 0; j < 27; j++) {
                o->pbuf[j] = buf[j];
                o->pbuf[27 + j] = m[j];
            }
            ((free_t)(USB_FREE | 1))(q[0], e.value);
            ((send_t)(USB_SEND | 1))(o->pbuf, 54);
            o->n_pack2++;
            return;
        }
    }
    ((send_t)(USB_SEND | 1))(buf, len);
    if (o->magic == OMAGIC)
        o->n_pack1++;
}

/* Paced 1 ms loops: the HID and USB tasks end each pass in osDelay(1), which blocks until the next RTOS tick;
 * a pass that crosses a tick boundary loses a whole tick (~900 passes/s measured). Keep a due time on the cycle
 * counter instead: behind schedule -> return at once (at most 2 passes in a row) so the missed pass is made up,
 * otherwise sleep as before; more than 4 ms off -> resync. */
typedef void (*delay_t)(uint32_t);

static void paced(int k, uint32_t ms)
{
    volatile struct out *o = OUT;
    if (o->magic == OMAGIC && ms == 1) {
        uint32_t now = DWT_CYC, due = o->due[k] + 1000 * CYC_US;
        int32_t late = (int32_t)(now - due);
        if (late > 4000 * CYC_US || late < -4000 * CYC_US)
            due = now;
        o->due[k] = due;
        if (late >= 0 && o->skips[k] < 2) {
            o->skips[k]++;
            o->n_catchup[k]++;
            return;
        }
        o->skips[k] = 0;
    }
    ((delay_t)(OS_DELAY | 1))(ms);
}

void out_delay_hid(uint32_t ms) { paced(0, ms); }
void out_delay_usb(uint32_t ms) { paced(1, ms); }
_Static_assert(sizeof(struct out) < 0x1000, "engine state must fit 0x2003C000-0x2003CFFF");
