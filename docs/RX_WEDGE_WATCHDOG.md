# RX wedge watchdog

The NV100 RX FIFO can wedge: the card replays old buffers instead of new samples. It
shows in the `dpmg_svc` log as `[RXGAP]` lines whose timestamp jumps back and forth by a
fixed amount (+/-8339456 samples at 30.72 Msps on a stock driver). Detection on its own
pauses the detector, sets `FLAG_RX_STALE` in STATUS and shows "RX STALE" in the GUI. The
watchdog in `dpmg/radio/dpmg_service.py` then recovers the radio in stages:

| Stage | Trigger | Action |
|---|---|---|
| 0 | RX replaying | Watch only. Brief replays that clear by themselves are left alone. |
| 1 | Still stale after 2 s | Restart the card's RX stream (`skiq_stop/start_rx_streaming` on the RX card). TX keeps running. Takes milliseconds. |
| 2 | Stale again 3 s after stage 1 | Full re-init with the last init parameters, restart RX, and resume TX in the same mode if it was on. RX is down for the length of an init (about 20 s on the G40). |
| 3 | Stale 10 s after the re-init | Stop trying and log `power-cycle the radio`. |

An episode ends after 2 s with no replay. Log lines start with `[WEDGE]`. The selftest
(see Commands) exercises stages 1 and 2. It cannot simulate a wedge that survives a
re-init, so stage 3 is not covered by it.

## Settings (environment variables on `dpmg_svc`)

| Variable | Default | Meaning |
|---|---|---|
| `DPMG_WEDGE_WATCHDOG` | `1` | `0` disables recovery (detection still works) |
| `DPMG_WEDGE_STREAM_RESTART_S` | `2.0` | Stale time before stage 1 |
| `DPMG_WEDGE_REINIT_AFTER_S` | `3.0` | Time after stage 1 before stage 2 |
| `DPMG_WEDGE_GIVEUP_AFTER_S` | `10.0` | Time after stage 2 before giving up |

`dpmg/radio/skiq_bridge.py` also reads three TX test overrides: `DPMG_TX_MODE=sync`,
`DPMG_TX_BLOCK=<samples>` and `DPMG_TX_ASYNC_PRIO=<prio>`. All are empty by default, and
`deploy/radio/g40_up_r36.sh` passes them through. Synchronous TX gives no RX gaps at all,
but the Python TX path underruns at 30.72 Msps.

## Commands

- `wedge_status` returns `enabled` and the counters `episodes`, `self_cleared`,
  `stream_restarts`, `reinits`, `giveups`, and `last` (start time, stale duration,
  highest stage reached).
- `selftest_rx_wedge {"hold": 8}` makes RX look wedged for `hold` seconds so the recovery
  can be tested. RX must be streaming. With `"survives_stream": true` the simulated wedge
  survives the stage 1 restart, which forces a stage 2 re-init. The selftest has no RF
  effect, but stage 2 does re-initialize the radio.

## Cause and driver fix

The wedge happens when the Epiq `dmadriver` RX DMA descriptor ring is lapped while the
other card transmits. NV100 RX blocks are 1018 samples, and the stock driver allocates
8192 RX descriptors: 8192 x 1018 = 8,339,456 samples, the size of every wedge jump. When
RX DMA completions stall during async TX, the DMA engine laps the ring, and the reader
sees a whole ring skipped or replayed.

The fix is a bigger ring of 65536 descriptors, eight times the stock one, so the DMA
engine has far more room before it laps the reader. Forward skips of one ring (about
2.2 s at 30.72 Msps) are still possible. The setting lives in
`/home/sidekiq/sidekiq_image_current/driver/r36.5.0/driver_config` on the G40:

    options dmadriver DMADescriptorsPerEngine=65536 RingBufferPacketCount=16384

At boot the Epiq loader (`/etc/init.d/sidekiq`, through `load_sidekiq_drivers.sh`) runs
`modprobe --config <that file>`. Delete the file to go back to the stock ring. To reload
the module without a reboot, run these three lines on the G40. They stop the radio
service briefly.

```bash
docker stop -t 30 dpmg_svc
docker run --rm --privileged --pid=host -v /:/host dpmg:latest chroot /host sh -c "rmmod dmadriver; insmod /home/sidekiq/sidekiq_image_current/driver/r36.5.0/5.15.185-tegra/dmadriver.ko DMADescriptorsPerEngine=65536 RingBufferPacketCount=16384"
docker start dpmg_svc
```

The watchdog stays in place either way.
