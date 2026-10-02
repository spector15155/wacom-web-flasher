/* Minimal native output: Wacom's own pen report for every S1 result (step 24) and every S2 result (step 29),
 * nothing else. The step-28 result (S2 pass a: no new measurement) is dropped, so the host gets two reports per
 * scan loop (~400/s), each sent the moment Wacom has processed its frame. With the S2 engine injecting, the step-29
 * result carries the second real position of the loop; when the injection is skipped (pen crossing a coil in fast
 * strokes) it is Wacom's own result of that frame, so fast strokes keep their cadence instead of a 5 ms hole.
 * Signal dips in hover (Wacom clears the in-range bit while still tracking) are passed on as in range.
 * No re-timing, interpolation, prediction or diagnostics. */
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
};

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

/* ring push entry: remember which step pushed the frame into this slot */
void out_push(void)
{
    volatile struct nat *o = NAT;
    if (o->magic != NMAGIC) {
        uint8_t *p = (uint8_t *)o;
        for (unsigned i = 0; i < sizeof(struct nat); i++)
            p[i] = 0;
        o->magic = NMAGIC;
    }
    uint16_t w = RING_W;
    if (w < 3)
        o->step[w] = STEPB;
}

/* calc ring pop: the slot at the read index is the frame being processed */
void out_pop(void)
{
    volatile struct nat *o = NAT;
    uint16_t r = RING_R;
    if (o->magic == NMAGIC && r < 3)
        o->cur = o->step[r];
}

/* calc mail put: queue (output X/Y, step) of this result for the HID task */
void out_put(void)
{
    volatile struct nat *o = NAT;
    if (o->magic != NMAGIC)
        return;
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

/* HID tick: wraps the stock pen routine; returns its record count / flags (low nibble = records) */
uint32_t out_hid(void)
{
    volatile struct nat *o = NAT;
    uint32_t n = ((fn0)(PEN_FN | 1))();
    volatile uint8_t *rec = (volatile uint8_t *)RECORDS;
    if (o->magic != NMAGIC || (n & 0x80))
        return n;                               /* not initialised yet / vendor report tick */
    o->ticks++;
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
    if (cnt != 1 || rec[0] != 0x10 || !(rec[1] & 0x20))
        return n;                               /* transitions / other reports: as they are */
    int32_t sx = rec[2] | rec[3] << 8 | rec[4] << 16, sy = rec[5] | rec[6] << 8 | rec[7] << 16;
    uint32_t w = o->qw, r = o->qr;
    if (w - r > NQ)
        r = w - NQ;
    for (uint32_t k = r; k != w; k++) {         /* oldest queued result with this output = this record */
        if (o->qx[k % NQ] == sx && o->qy[k % NQ] == sy) {
            o->qr = k + 1;
            if (o->qs[k % NQ] == 28) {
                o->n_drop++;
                return n & ~0xFu;               /* S2 pass a: no new measurement, no report */
            }
            o->n_sent++;
            return n;
        }
    }
    o->n_stock++;                               /* unmatched: Wacom's own record */
    return n;
}
