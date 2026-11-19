/* rxdrain.c: native RX batch drain for the Epiq (libsidekiq) backend.
 *
 * The NV100 RX block is fixed at 1018 samples (1024 words with
 * the 6-word header; libsidekiq 4.19-4.25 has no call to make it larger), so
 * 61.44 MSps needs 60,354 skiq_receive() calls a second, 16.57 us each. The
 * Python per-block loop took 17-20 us per block on the G40 and fell behind:
 * 85 overruns in 2 minutes at 61.44. With this loop in C the receive side runs
 * about 7x real time at 61.44 and 13x at 30.72, with 0 overruns in 10-minute
 * runs with and without TX keyed (measured on the G40).
 *
 * skiq_bridge.load_rxdrain() loads librxdrain.so from
 * native/lib/, else the repo root, else beside skiq_bridge.py
 * (DPMG_RXDRAIN_LIB overrides the path) and checks rxd_abi().
 * sdr_core.FullDuplexScheduler._rx_loop then calls skiq_drain_batch() once per
 * batch (up to 2048 blocks or 50 ms); ctypes releases the GIL for the whole
 * call. The loop calls skiq_receive() until the card is empty, the block cap
 * or the time budget is reached, and writes complex64 (interleaved float32,
 * scale 1/32768, optional QI to IQ swap) straight into the caller's buffer.
 * The output is bit-identical to the Python path (int16 to float32 is exact
 * and the scale is a power of two). Without the library, or with
 * DPMG_RX_NATIVE=0, the Python path runs. At RX start the service log says
 * which: "[rx] drain engine: native" or "python".
 *
 * skiq_receive is passed in as a function pointer taken from the libsidekiq
 * the bridge already loaded, so this builds without the Epiq headers. On the
 * radio, inside the dpmg image (native/build.sh has the docker line):
 *
 *     gcc -O2 -shared -fPIC -o native/lib/librxdrain.so native/rxdrain.c
 *
 * Bump RXD_ABI whenever rxd_state_t or the skiq_drain_batch() signature
 * changes, and update _RxdState / RXDRAIN_ABI in skiq_bridge.py to match.
 *
 * Timestamp checks match the Python path: st->n_mismatch counts every block
 * whose rf_timestamp != previous + previous length (the [RXGAP] lines),
 * st->n_fwd the forward jumps (counted as overruns), st->n_back the backward
 * jumps (a replaying FIFO, the wedge watchdog's input). The expectation
 * (last_ts/last_n) carries across calls; last_n = 0 means none yet.
 *
 * Tests: tests/test_rx_native_drain.py runs both engines on a scripted
 * skiq_receive stub and checks identical samples and gap counts.
 */
#include <stdint.h>
#include <string.h>
#include <time.h>

#define RXD_ABI 1
#define HDR_BYTES 24            /* SKIQ_RX_HEADER_SIZE_IN_BYTES */
#define MAX_I16 2036            /* (SKIQ_MAX_RX_BLOCK_SIZE_IN_BYTES - header) / 2 */

typedef int32_t (*skiq_receive_fn)(uint8_t, int *, void **, uint32_t *);

typedef struct {
    /* in/out across calls */
    uint64_t last_ts;
    uint64_t last_n;            /* samples in the previous block, 0 = no expectation */
    /* out, this call */
    uint64_t ts0;               /* rf_timestamp of the first block */
    uint64_t n_floats;          /* float32 values written (2 per sample) */
    uint32_t n_blocks;
    uint32_t n_mismatch;
    uint32_t n_fwd;
    uint32_t n_back;
    uint64_t first_exp;         /* first mismatch: expected ts */
    uint64_t first_got;         /*                 and the ts that came */
    int32_t  empty;             /* 1 = stopped because the card had no block */
    int32_t  full;              /* 1 = stopped because the next block would not fit */
} rxd_state_t;

int32_t rxd_abi(void) { return RXD_ABI; }

static double now_s(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec + 1e-9 * (double)t.tv_nsec;
}

int32_t skiq_drain_batch(void *fn, uint8_t card, float *dst, uint64_t cap_floats,
                         uint32_t max_blocks, double budget_s, int32_t swap_iq,
                         rxd_state_t *st)
{
    skiq_receive_fn rx = (skiq_receive_fn)fn;
    const float k = 1.0f / 32768.0f;
    const double t0 = now_s();
    uint64_t w = 0;
    uint32_t nb = 0;

    st->n_floats = 0; st->n_blocks = 0; st->n_mismatch = 0; st->n_fwd = 0;
    st->n_back = 0; st->first_exp = 0; st->first_got = 0; st->empty = 0; st->full = 0;
    st->ts0 = 0;

    while (nb < max_blocks) {
        if (nb && (nb & 63) == 0 && now_s() - t0 > budget_s)
            break;
        /* room for a largest block before receiving it: a block taken from the
           card must never be dropped for want of space */
        if (cap_floats - w < MAX_I16) { st->full = 1; break; }
        int hdl = 0;
        void *p = 0;
        uint32_t len = 0;
        if (rx(card, &hdl, &p, &len) != 0 || !p) { st->empty = 1; break; }
        if (len <= HDR_BYTES) { st->empty = 1; break; }
        uint64_t n_i16 = (uint64_t)(len - HDR_BYTES) / 2;
        if (n_i16 <= 1) { st->empty = 1; break; }
        if (n_i16 > MAX_I16) n_i16 = MAX_I16;     /* larger than the API maximum: cannot happen */

        uint64_t ts;
        memcpy(&ts, p, sizeof ts);
        if (st->last_n) {
            uint64_t exp = st->last_ts + st->last_n;
            if (ts != exp) {
                if (st->n_mismatch == 0) { st->first_exp = exp; st->first_got = ts; }
                st->n_mismatch++;
                if (ts > exp) st->n_fwd++;
            }
            if (ts < st->last_ts) st->n_back++;
        }
        if (nb == 0) st->ts0 = ts;

        const int16_t *s = (const int16_t *)((const uint8_t *)p + HDR_BYTES);
        float *d = dst + w;
        if (swap_iq) {
            for (uint64_t i = 0; i + 1 < n_i16; i += 2) {
                d[i] = (float)s[i + 1] * k;
                d[i + 1] = (float)s[i] * k;
            }
        } else {
            for (uint64_t i = 0; i < n_i16; i++)
                d[i] = (float)s[i] * k;
        }
        w += n_i16;
        st->last_ts = ts;
        st->last_n = n_i16 / 2;
        nb++;
    }
    st->n_floats = w;
    st->n_blocks = nb;
    return (int32_t)nb;
}
