/* Minimal native output: Wacom's own pen report for every S1 result (step 24) and every S2 result (step 29),
 * nothing else. The step-28 result (S2 pass a: no new measurement) is dropped, so the host gets two reports per
 * scan loop (~400/s), each sent the moment Wacom has processed its frame. With the S2 engine injecting, the step-29
 * result carries the second real position of the loop; when the injection is skipped (pen crossing a coil in fast
 * strokes) it is Wacom's own result of that frame, so fast strokes keep their cadence instead of a 5 ms hole.
 * Signal dips in hover (Wacom clears the in-range bit while still tracking) are passed on as in range.
 * No re-timing, interpolation, prediction or diagnostics. REPEAT: every HID tick without a new real report re-sends
 * the last one (max report rate, copies only); with DOUBLE two per tick, packed two per USB packet (2000/s). */
#include <stdint.h>

#define CALC     ((volatile uint8_t *)0x20010E0C)
#define RING_W   (*(volatile uint16_t *)(0x2000F8E8 + 0x151E))
#define RING_R   (*(volatile uint16_t *)(0x2000F8E8 + 0x1520))
#define STEPB    (*(volatile uint8_t *)STEP_ADDR)
#define NAT      ((volatile struct nat *)0x2003C000)   /* 0x2003C000-0x2003CFFF: no references in the stock image */
#define NMAGIC   0x4E415432u
#define NQ       8                       /* calc results waiting for their record */

struct nat {
    uint32_t magic;
    uint8_t step[3];                    /* ring slot -> scan step that pushed the frame */
    uint8_t cur;                        /* step of the frame the calc is processing */
    uint32_t qw, qr;                    /* result queue: written by the calc task, read by the HID task */
    int32_t qx[NQ], qy[NQ];
    uint8_t qs[NQ];
    uint32_t n_sent, n_drop, n_stock;
    uint32_t ticks, last_in;            /* HID ticks (1 ms), tick of the last in-range record from Wacom */
    uint32_t n_kept;                    /* dip records passed on as in range */
    uint32_t log_n;                     /* NATLOG entries written */
    int32_t lx, ly;                     /* last in-range position passed on */
    /* REPEAT: last real in-range report, re-sent on every tick until the next one (no interpolation) */
    uint8_t last[27], last_ok, pad[4];
    uint32_t last_t, n_rep;
    /* USB packing / paced loops (REPEAT with DOUBLE) */
    uint8_t pbuf[64];
    uint32_t due[2];
    uint8_t skips[2], pad2[2];
    uint32_t n_pack2, n_pack1;
    uint32_t n_multi;                   /* ticks that sent two calc results */
    uint32_t n_push, n_pop;             /* frames pushed to / taken from the calc ring (CALCDRAIN) */
    uint8_t cskip, pad3[3];
    uint32_t n_cdrain;
    uint8_t held[27], has_held, pad4[4];  /* PAIR: single in-range record waiting for a partner */
    uint32_t n_held, n_alone;
    uint32_t n_epwait_loops, n_eptimeout;
    int32_t erx[32], ery[32];           /* EDGESM: positions sent, per-axis smoothing history */
    uint32_t en, elast, ewin[2], n_edge;
    uint8_t fifo[4][56];                /* USBFIFO: packets waiting for the pen endpoint */
    uint8_t flen[4], inflight, pad6[3];
    uint32_t fh, ft, n_fdrop, n_fsent;
    uint32_t n_mailwait, n_mailfull;
    /* NATDIAG: per pen-routine call that took a calc result: [state +0x791 & 3][+0x793 == 0x0C][record built] */
    uint32_t dg[4][2][2];
    uint32_t dg_lvl[4][2];              /* [level +0x794 (0,1,2,3+)][record built] */
    uint32_t dg_calls, dg_got;
    uint8_t v24, pad7[3];               /* KEEPVALID: last S1 (step 24) result had valid X and Y */
    uint32_t n_revalid;
};

#ifndef NODROP
#define NODROP 0
#endif

#ifndef DOUBLE
#define DOUBLE 0
#endif
#ifndef REPEAT_MS
#define REPEAT_MS 20                    /* no new real report for this long: stop repeating (calc stalled) */
#endif

#ifdef NATLOG
/* diagnostic: first 2048 in-range calc results after boot, 16 B each at 0x20034000 (below NAT):
 * +0 step, +1 low byte of the S2 engine's inject counter, +4 DWT cycles, +8 output X/Y u16, +12 per-scan X/Y u16 */
#define NLOGA 0x20034000u
#define NLOGN 2048u
#endif

#ifndef KEEP_MS
#define KEEP_MS 150
#endif

typedef uint32_t (*fn0)(void);
void s2x_restore(void);

/* ring push entry: remember which step pushed the frame into this slot */
void out_push(void)
{
    volatile struct nat *o = NAT;
    if (o->magic != NMAGIC) {
        uint8_t *p = (uint8_t *)o;
        for (unsigned i = 0; i < sizeof(struct nat); i++)
            p[i] = 0;
        o->magic = NMAGIC;
        *(volatile uint32_t *)0xE000EDFC |= 1u << 24;   /* DWT cycle counter (paced loops) */
        *(volatile uint32_t *)0xE0001000 |= 1u;
    }
    uint16_t w = RING_W;
    if (w < 3)
        o->step[w] = STEPB;
    o->n_push++;
#ifdef RESTORE
    if (STEPB == 29)
        s2x_restore();                          /* the stage holds the injected frame: S1 back in the work area */
#endif
}

/* calc ring pop: the slot at the read index is the frame being processed */
void out_pop(void)
{
    volatile struct nat *o = NAT;
    uint16_t r = RING_R;
    if (o->magic == NMAGIC && r < 3)
        o->cur = o->step[r];
    if (o->magic == NMAGIC)
        o->n_pop++;
}

#ifdef CALCDRAIN
/* calc task loop delay (osDelay(1) with --calc1): the calc takes one frame per pass, so frames pushed in a burst
 * (up to 7 per loop) overflow its 3-slot ring. While frames are waiting, the next pass runs at once (at most 3
 * in a row), otherwise it sleeps as before. */
typedef void (*cdelay_t)(uint32_t);
void out_delay_calc(uint32_t ms)
{
    volatile struct nat *o = NAT;
    if (o->magic == NMAGIC) {
        uint32_t pend = o->n_push - o->n_pop;
        if (pend && pend < 8 && o->cskip < 3) {
            o->cskip++;
            o->n_cdrain++;
            return;
        }
        if (pend >= 8)
            o->n_pop = o->n_push;               /* pushes the ring refused: resync */
        o->cskip = 0;
    }
    ((cdelay_t)(OS_DELAY | 1))(ms);
}
#endif

#ifdef MAILWAIT
/* The calc hands each result to the HID task in a mail block from a small pool. Stock makes ~200 results/s and the
 * queue never fills; at ~1500/s in bursts it can, and a failed put appears to lose the block for good: after a few
 * (typically around pen exit / re-entry) one block is left and the HID task gets ~330 results/s until reboot
 * (v3.72: 1500/s, then 330/s after the pen came back). Before the put, while the queue is full, the calc sleeps
 * 1 ms (at most 4 times) so the HID task can take one first. Queue: FreeRTOS Queue_t at [mail cb + 4],
 * uxMessagesWaiting +0x38, uxLength +0x3C. */
typedef void (*mdelay_t)(uint32_t);
static void mail_wait(volatile struct nat *o)
{
    volatile uint32_t *cb = *(volatile uint32_t *volatile *)MAILQ;
    if (!cb)
        return;
    volatile uint8_t *q = (volatile uint8_t *)cb[1];
    if (!q)
        return;
    for (int i = 0; i < 4; i++) {
        uint32_t n = *(volatile uint32_t *)(q + 0x38), len = *(volatile uint32_t *)(q + 0x3C);
        if (!len || n < len)
            return;
        o->n_mailwait++;
        ((mdelay_t)(OS_DELAY | 1))(1);
    }
    o->n_mailfull++;
}
#endif

/* calc mail put: queue (output X/Y, step) of this result for the HID task. The call stub keeps r0-r3: r1 is the
 * mail block (copy of the calc output) about to be put. */
void out_put(uint32_t mq, volatile uint8_t *mail)
{
    volatile struct nat *o = NAT;
    (void)mq;
    if (o->magic != NMAGIC)
        return;
#ifdef KEEPVALID
    /* In some state (entered e.g. after the pen re-enters, then kept) Wacom's calc clears the X / Y valid bits
     * (+0x18 / +0x50 bit 0) of level-3 results from the extra frames (steps 23, 25-28), and its pen routine skips
     * them: ~350 reports/s instead of ~1500 (v3.77 counters: level 2 all reported, level 3 ~950/s skipped). Those
     * results carry Wacom's own filtered position; keep them valid when this loop's S1 result was valid. */
    if (mail) {
        uint8_t st = o->cur;
        int valid = (mail[0x18] & 1) && (mail[0x50] & 1);
        if (st == 24)
            o->v24 = (uint8_t)valid;
        else if (!valid && o->v24 && st != 29 && mail[0x794] >= 2 && (st == 23 || (st >= 25 && st <= 28))) {
            mail[0x18] |= 1;
            mail[0x50] |= 1;
            o->n_revalid++;
        }
    }
#endif
#ifdef MAILWAIT
    mail_wait(o);
#endif
    uint32_t w = o->qw;
    o->qx[w % NQ] = *(volatile int32_t *)(CALC + 0x1C);
    o->qy[w % NQ] = *(volatile int32_t *)(CALC + 0x54);
    o->qs[w % NQ] = o->cur;
    o->qw = w + 1;
#ifdef NATLOG
    int32_t ox = o->qx[w % NQ], oy = o->qy[w % NQ];
    if (o->log_n < NLOGN && ox > 0 && oy > 0) {
        volatile uint8_t *e = (volatile uint8_t *)(NLOGA + 16 * o->log_n);
        if (o->log_n == 0) {
            *(volatile uint32_t *)0xE000EDFC |= 1u << 24;
            *(volatile uint32_t *)0xE0001000 |= 1u;
        }
        e[0] = o->cur;
        e[1] = *(volatile uint8_t *)(0x2003F780 + 0x2C);
        *(volatile uint32_t *)(e + 4) = *(volatile uint32_t *)0xE0001004;
        *(volatile uint16_t *)(e + 8) = (uint16_t)ox;
        *(volatile uint16_t *)(e + 10) = (uint16_t)oy;
        *(volatile uint16_t *)(e + 12) = (uint16_t)(*(volatile int32_t *)(CALC + 0xFA4) - 2800);
        *(volatile uint16_t *)(e + 14) = (uint16_t)(*(volatile int32_t *)(CALC + 0x10D8) - 2840);
        o->log_n++;
    }
#endif
}

#ifdef REPEAT
/* tick without a new real report: the last one again (two copies per tick with DOUBLE) */
static uint32_t repeat(volatile struct nat *o, volatile uint8_t *rec, uint32_t n)
{
    for (int j = 0; j < 27; j++)
        rec[j] = o->last[j];
    o->n_rep++;
    if (DOUBLE) {
        for (int j = 0; j < 27; j++)
            rec[27 + j] = o->last[j];
        return (n & ~0xFu) | 2;
    }
    return (n & ~0xFu) | 1;
}

/* a new real in-range report goes out: remember it (with DOUBLE, the tick's second report is the same) */
static uint32_t sent(volatile struct nat *o, volatile uint8_t *rec, uint32_t n)
{
    for (int j = 0; j < 27; j++)
        o->last[j] = rec[j];
    o->last_ok = 1;
    o->last_t = o->ticks;
    if (DOUBLE) {
        for (int j = 0; j < 27; j++)
            rec[27 + j] = rec[j];
        return (n & ~0xFu) | 2;
    }
    return n;
}
#endif

/* HID tick: wraps the stock pen routine; returns its record count / flags (low nibble = records) */
static uint32_t handle(volatile struct nat *o, uint32_t n)
{
    volatile uint8_t *rec = (volatile uint8_t *)RECORDS;
    uint32_t cnt = n & 0xF;
    for (uint32_t k = 0; k < cnt && k < 4; k++) {
        volatile uint8_t *r = rec + 27 * k;
        if (r[0] != 0x10)
            continue;
        int32_t x = r[2] | r[3] << 8 | r[4] << 16, y = r[5] | r[6] << 8 | r[7] << 16;
        if (r[1] & 0x20) {
            o->last_in = o->ticks;
            o->lx = x;
            o->ly = y;
        } else if ((r[1] & 0x40) && o->ticks - o->last_in <= KEEP_MS && (x | y) != 0) {
            /* far-hover glitch guard: Wacom's output can jump ~2800 counts toward the corner for a result or two */
            int32_t dx = x - o->lx, dy = y - o->ly;
            if ((dx < 0 ? -dx : dx) + (dy < 0 ? -dy : dy) > 2500)
                continue;
            o->lx = x;
            o->ly = y;
            /* signal dip: in fast or high hover Wacom clears the in-range bit for ~10-35 ms while it keeps tracking
             * the pen (these records carry its moving real positions, often two per tick at the transition). Within
             * KEEP_MS of the last in-range record they go out as in range; a real lift-off ends range KEEP_MS later
             * (dips in fast / high sweeps last up to ~120 ms). */
            r[1] |= 0x20;
            o->n_kept++;
        }
    }
    if (cnt != 1 || rec[0] != 0x10 || !(rec[1] & 0x20)) {
#ifdef REPEAT
        if (cnt == 0 && o->last_ok && o->ticks - o->last_t <= REPEAT_MS)
            return repeat(o, rec, n);
        for (uint32_t k = 0; k < cnt && k < 4; k++)
            if (rec[27 * k] == 0x10 && !(rec[27 * k + 1] & 0x20))
                o->last_ok = 0;                 /* the pen left: stop repeating */
#endif
        return n;                               /* transitions / other reports: as they are */
    }
    int32_t sx = rec[2] | rec[3] << 8 | rec[4] << 16, sy = rec[5] | rec[6] << 8 | rec[7] << 16;
    uint32_t w = o->qw, r = o->qr;
    if (w - r > NQ)
        r = w - NQ;
    for (uint32_t k = r; k != w; k++) {         /* oldest queued result with this output = this record */
        if (o->qx[k % NQ] == sx && o->qy[k % NQ] == sy) {
            o->qr = k + 1;
            if (!NODROP && o->qs[k % NQ] == 28) {
                o->n_drop++;
#ifdef REPEAT
                if (o->last_ok)
                    return repeat(o, rec, n);
#endif
                return n & ~0xFu;               /* S2 pass a: no new measurement, no report */
            }
            o->n_sent++;
#ifdef REPEAT
            return sent(o, rec, n);
#else
            return n;
#endif
        }
    }
    o->n_stock++;                               /* unmatched: Wacom's own record */
#ifdef REPEAT
    return sent(o, rec, n);
#else
    return n;
#endif
}

#ifdef EDGESM
/* top / left strip (Wacom's coil window 0: it extrapolates, 2-4x the centre's jitter when slow; S2 cannot be used
 * there): per-axis average of the last reports, by speed, as v3.15 (edge p90 54 -> 16 hover, 59 -> 11.5 tip) with
 * point counts doubled for ~1500 reports/s. Full weight below EDGE_IN, none above EDGE_OUT; up to 16 points when
 * still, 1 (none) when fast; the window moves +-1 per report. */
#ifndef EDGE_IN
#define EDGE_IN  5000
#endif
#ifndef EDGE_OUT
#define EDGE_OUT 8000
#endif
#define ENR 32
static int32_t esm(volatile struct nat *o, int ax, volatile int32_t *ring, uint32_t n, int32_t v)
{
    int32_t w = v <= EDGE_IN ? 256 : v >= EDGE_OUT ? 0 : (EDGE_OUT - v) * 256 / (EDGE_OUT - EDGE_IN);
    int32_t d = n >= 8 ? v - ring[(n - 8) % ENR] : 0;
    if (d < 0)
        d = -d;
    uint32_t want = d < 40 ? 16 : d < 100 ? 12 : d < 200 ? 8 : d < 400 ? 4 : 1;
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
        sum += ring[(n - k) % ENR];
    o->n_edge++;
    return v + (sum / (int32_t)m - v) * w / 256;
}

/* one outgoing report (a copy on its way to USB: Wacom's record buffer is read back by its pen routine, v3.74
 * smoothed that buffer and the routine then emitted only ~350 reports/s) */
static void edge_filter(volatile struct nat *o, volatile uint8_t *r)
{
    {
        if (r[0] != 0x10)
            return;
        if (!(r[1] & 0x20)) {
            o->en = 0;
            return;
        }
        if (o->ticks - o->elast > 20)
            o->en = 0;                          /* new approach: fresh history */
        o->elast = o->ticks;
        int32_t x = r[2] | r[3] << 8 | r[4] << 16, y = r[5] | r[6] << 8 | r[7] << 16;
        uint32_t i = o->en;
        o->erx[i % ENR] = x;
        o->ery[i % ENR] = y;
        o->en = i + 1;
        x = esm(o, 0, o->erx, i, x);
        y = esm(o, 1, o->ery, i, y);
        r[2] = (uint8_t)x; r[3] = (uint8_t)(x >> 8); r[4] = (uint8_t)(x >> 16);
        r[5] = (uint8_t)y; r[6] = (uint8_t)(y >> 8); r[7] = (uint8_t)(y >> 16);
    }
}
#endif

static uint32_t out_hid_core(volatile struct nat *o);

uint32_t out_hid(void)
{
    volatile struct nat *o = NAT;
    return out_hid_core(o);
}

#ifdef NATDIAG
static uint32_t pen_call(volatile struct nat *o)
{
    uint32_t n = ((fn0)(PEN_FN | 1))();
    if (o->magic != NMAGIC || (n & 0x80))
        return n;
    o->dg_calls++;
    volatile uint8_t *mv = (volatile uint8_t *)MAILQ;
    if (mv[6] == 1) {                           /* a calc result was taken this call */
        volatile uint8_t *cur = (volatile uint8_t *)CUR_ADDR;
        int st = cur[0x791] & 3, c = cur[0x793] == 0x0C, built = (n & 0xF) != 0;
        int lv = cur[0x794] > 3 ? 3 : cur[0x794];
        o->dg[st][c][built]++;
        o->dg_lvl[lv][built]++;
        o->dg_got++;
    }
    return n;
}
#define PEN_CALL(o) pen_call(o)
#else
#define PEN_CALL(o) ((fn0)(PEN_FN | 1))()
#endif

static uint32_t out_hid_core(volatile struct nat *o)
{
    uint32_t n = PEN_CALL(o);
    if (o->magic != NMAGIC || (n & 0x80))
        return n;                               /* not initialised yet / vendor report tick */
    o->ticks++;
    n = handle(o, n);
#ifdef MULTI
    /* more calc results waiting than one 1 ms tick takes: run the pen routine once more and send both records
     * (the USB task packs two pen reports per packet) */
    volatile uint8_t *rec = (volatile uint8_t *)RECORDS;
    if ((n & 0xF) == 1 && rec[0] == 0x10 && (rec[1] & 0x20) && o->qw != o->qr) {
        uint8_t tmp[27];
        for (int j = 0; j < 27; j++)
            tmp[j] = rec[j];
        uint32_t n2 = PEN_CALL(o);
        if (!(n2 & 0x80))
            n2 = handle(o, n2);
        uint32_t c2 = n2 & 0xF;
        if ((n2 & 0x80) || c2 == 0) {
            for (int j = 0; j < 27; j++)        /* nothing more: the first record alone */
                rec[j] = tmp[j];
            return n;
        }
        if (c2 == 1) {
            for (int j = 0; j < 27; j++) {
                rec[27 + j] = rec[j];
                rec[j] = tmp[j];
            }
            o->n_multi++;
            return (n2 & ~0xFu) | 2;
        }
        return n2;                              /* a transition with several records: those go out */
    }
#endif
#ifdef PAIR
    /* every USB packet carries two pen reports: a packet with a single report was often lost on the way to the host
     * (v3.67: host got ~113/s while the firmware sent ~870 single + ~113 paired packets/s). A lone in-range record
     * waits one tick and goes out with the next one; with nothing new next tick it goes alone. */
    {
        volatile uint8_t *rec = (volatile uint8_t *)RECORDS;
        uint32_t c = n & 0xF;
        int lone = c == 1 && rec[0] == 0x10 && (rec[1] & 0x20);
        if (o->has_held) {
            o->has_held = 0;
            if (c == 1) {                       /* held + this one */
                for (int j = 0; j < 27; j++) {
                    rec[27 + j] = rec[j];
                    rec[j] = o->held[j];
                }
                return (n & ~0xFu) | 2;
            }
            if (c == 0) {                       /* nothing new: the held one alone */
                for (int j = 0; j < 27; j++)
                    rec[j] = o->held[j];
                o->n_alone++;
                return (n & ~0xFu) | 1;
            }
            if (c == 2) {                       /* two already: held + first now, second waits */
                uint8_t t[27];
                for (int j = 0; j < 27; j++) {
                    t[j] = rec[27 + j];
                    rec[27 + j] = rec[j];
                    rec[j] = o->held[j];
                }
                for (int j = 0; j < 27; j++)
                    o->held[j] = t[j];
                o->has_held = 1;
                return (n & ~0xFu) | 2;
            }
            return n;                           /* 3+ records (transition): drop the held one */
        }
        if (lone) {
            for (int j = 0; j < 27; j++)
                o->held[j] = rec[j];
            o->has_held = 1;
            o->n_held++;
            return n & ~0xFu;
        }
    }
#endif
    return n;
}

#if defined(REPEAT) || defined(MULTI)
/* USB pen report send (usbif task): if a second pen report is queued, send both in one packet (64 B endpoint, the
 * host HID class splits a transfer holding two reports). Replaces send(buf, 27). Same as out.c. */
struct evt { uint32_t status, value, def; };
typedef void (*get_t)(struct evt *, uint32_t, uint32_t);
typedef uint32_t (*free_t)(uint32_t, uint32_t);
typedef void (*send_t)(volatile uint8_t *, uint32_t);

/* ST's HID SendReport drops the report (returns busy) while the previous transfer on the pen endpoint is still
 * waiting for the host's poll; at ~1500 reports/s the send timing drifts against the host's 1 ms polling and whole
 * packets were lost in some phases (v3.71: host 737/s while 1500/s were produced). Wait for the endpoint instead
 * (at most one poll interval, 1.5 ms timeout): nothing is dropped and the sends lock onto the host's polling. */
/* ST's HID SendReport drops a report (returns busy) while the previous transfer is still waiting for the host's
 * poll. v3.72 busy-waited for the endpoint in the USB task: in some phases of the send timing vs the host's 1 ms
 * polling it spun most of the time and starved the HID task (~350-400 reports/s). Now outgoing packets go into a
 * small FIFO; one is handed to the endpoint only when it is idle, and the USB task retries on every 1 ms pass (it
 * sleeps in between as stock). A packet stays in its slot until its transfer completed (the endpoint reads it). */
static int ep_free(void)
{
    volatile uint8_t *dev = (volatile uint8_t *)USBDEV;
    if (dev[0x1FC] != 3)
        return 1;                               /* not configured: let the stock send decide */
    volatile uint8_t *cls = *(volatile uint8_t *volatile *)(dev + 0x218);
    return !cls || cls[0x210] == 0;
}

static void fifo_pump(volatile struct nat *o)
{
    if (!ep_free())
        return;
    if (o->inflight) {                          /* the packet in flight has been collected */
        o->inflight = 0;
        o->fh++;
    }
    if (o->fh != o->ft) {
        uint32_t k = o->fh % 4;
        ((send_t)(USB_SEND | 1))(o->fifo[k], o->flen[k]);
        o->inflight = 1;
        o->n_fsent++;
    }
}

static volatile uint8_t *fifo_slot(volatile struct nat *o)
{
    if (o->ft - o->fh >= 4) {
        o->n_fdrop++;
        return 0;
    }
    return o->fifo[o->ft % 4];
}

void out_usb_pen(uint8_t *buf, uint32_t len)
{
    volatile struct nat *o = NAT;
    if (o->magic != NMAGIC || len > 56) {
        ((send_t)(USB_SEND | 1))(buf, len);
        return;
    }
    volatile uint32_t *q = (volatile uint32_t *)USBQ;
    volatile uint8_t *d = fifo_slot(o);
    if (!d) {
        fifo_pump(o);
        return;
    }
#ifdef EDGESM
    if (len == 27)
        edge_filter(o, buf);                    /* reports reach this in sending order */
#endif
    for (uint32_t j = 0; j < len; j++)
        d[j] = buf[j];
    uint32_t n = len;
    if (len == 27 && buf[0] == 0x10 && ((volatile uint8_t *)q)[4] == 1) {
        struct evt e;
        ((get_t)(USB_GET | 1))(&e, q[0], 0);
        if (e.status == 0x20) {
            volatile uint8_t *m = (volatile uint8_t *)e.value;
            for (int j = 0; j < 27; j++)
                d[27 + j] = m[j];
            ((free_t)(USB_FREE | 1))(q[0], e.value);
#ifdef EDGESM
            edge_filter(o, d + 27);
#endif
            n = 54;
            o->n_pack2++;
        }
    }
    if (n == 27)
        o->n_pack1++;
    o->flen[o->ft % 4] = (uint8_t)n;
    o->ft++;
    fifo_pump(o);
}

/* paced 1 ms HID / USB loops (see out.c): a pass that misses its tick is made up at once */
#define CYC_US   96
#define DWT_CYC  (*(volatile uint32_t *)0xE0001004)
typedef void (*delay_t)(uint32_t);

static void paced(int k, uint32_t ms)
{
    volatile struct nat *o = NAT;
    if (o->magic == NMAGIC && k == 1)
        fifo_pump(o);                           /* USB task pass: hand a waiting packet to the endpoint */
    if (o->magic == NMAGIC && ms == 1) {
        uint32_t now = DWT_CYC, due = o->due[k] + 1000 * CYC_US;
        int32_t late = (int32_t)(now - due);
        if (late > 4000 * CYC_US || late < -4000 * CYC_US)
            due = now;
        o->due[k] = due;
        if (late >= 0 && o->skips[k] < 2) {
            o->skips[k]++;
            return;
        }
        o->skips[k] = 0;
    }
    ((delay_t)(OS_DELAY | 1))(ms);
}

void out_delay_hid(uint32_t ms) { paced(0, ms); }
void out_delay_usb(uint32_t ms) { paced(1, ms); }
#endif
_Static_assert(sizeof(struct nat) < 0x1000, "state must fit 0x2003C000-0x2003CFFF");
