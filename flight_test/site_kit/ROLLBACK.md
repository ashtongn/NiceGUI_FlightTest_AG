# Rollback: the payload G40 back on its pre-site code

For the site deploy ("Loading the latest code onto Payload 2" in the flight test field cards). The payload G40 runs the radio files of its last
deploy, 5 November 2026 09:09, on top of the whole tree of 3 November and the two deploys of
4 November, called "the 3 November code" below. The rollback puts back exactly what the deploy found on the unit, whatever
that was, from the backups `push_update.sh` makes on the radio.

## When

- Any check of the deploy reads NOT OK, or the service does not come up.
- 30 minutes have passed since `site_deploy.sh go` started and the final read is not done.
- The test lead calls it.

After a rollback the flight runs on the pre-site code. Then, on that code: the radio offers three
clock choices (internal, external, gpsdo); external there means the 5 November carrier path,
which does not use the 10 MHz and is refused unless the carrier was set to ext10M by hand; the
clock to fly on is internal, or gpsdo if a GPS antenna is fitted with the bias on. The 8080 GUI
works with the old service (it falls back to the old clock words). Radio log files keep the old
t_unix first column.

## What the deploy changed, so what has to go back

| Change | Where | Undone by |
|---|---|---|
| nine files replaced (`DEPLOY_SET.txt` of the bundle, native/lib/librxdrain.so among them) | `~/work/NiceGui_test` on the radio | copying the backups back: level 1 |
| dpmg_svc and dpmg-gpu made again by g40_up_r36.sh, with two extra read-only mounts (`/epiq`, `/usr/bin/hmc_from_file`) | Docker on the radio | nothing needed: the old code does not use the mounts. Level 2 only if the service will not run in the new container |
| only if the image lacked a module: dpmg:latest rebuilt with the wheels | Docker on the radio | level 2 puts the old dpmg:latest tag back |
| image `dpmg:payload-pre-site-<date>` added (copy of the old container) | Docker on the radio | left in place; it is the level 2 source |

Not changed by the deploy at all: `rf_frontend.yaml` (port map, payload guard, HPA 1189 lines; `site_no_tx_floor.sh` changes it on its own and keeps a dated copy beside it),
`data/`, the HMC7044 boot file, the NV100 EEPROM, nvpmodel. `native/lib/librxdrain.so` is pushed
with the code but should be the same build as before (SHA256 de8d9a24...); level 1 puts the old
copy back if there was one, and keeps the new one if the unit had none.

## Level 1: files back, container kept (about 2 minutes)

From the site kit folder on the laptop, the same results folder as the deploy:

```bash
bash site_rollback.sh files
```

It reads the backup folder from `deploy_state.env` (BACKUP_DIR), copies the nine files back,
runs `docker restart -t 60 dpmg_svc` (a clean stop: the HPA enable line is left disabled), waits
for "up: cmd_port=", then `site_read_state.sh rollback`. Expect:

- `ok      the deploy files are back to their before-deploy hashes`
- `ok      clock choices now: 3, expected 3`
- `same` on the port map, guard, front end and switch lines; `ok      HPA after: keyed False, enable line active False, switch tx False`
- `ok      rf_frontend.yaml unchanged`

The same by hand, if the laptop scripts cannot be used (the operator types the password):

```bash
ssh sidekiq@192.168.55.1
ls -t ~/work/dpmg_backups | head -3          # the deploy's folder: today's date, or today's date .2, .3 ...
B=~/work/dpmg_backups/<that folder>
cd ~/work/NiceGui_test
for f in dpmg/radio/dpmg_service.py dpmg/radio/g40_clock.py dpmg/radio/sdr_core.py dpmg/radio/skiq_bridge.py dpmg/radio/port_map.py deploy/radio/g40_up_r36.sh; do cp -p $B/$f $f; done
docker restart -t 60 dpmg_svc
docker logs --since 2m dpmg_svc 2>&1 | grep -E 'up: cmd_port=|Traceback'
sha256sum dpmg/radio/dpmg_service.py dpmg/radio/sdr_core.py dpmg/radio/skiq_bridge.py dpmg/radio/g40_clock.py
```

The last line, on the 5 November code, starts `5ebc2715`, `65b88320`, `17e09a3d`, `fe59edaa` (the
deploy record of 5 November); `before/radio_hashes.txt` in the results folder has what the deploy
actually found, which is the target if it differs.

## Level 2: the old container back (about 2 more minutes)

Only when the service does not come up after level 1 (a Traceback about a missing module, no
"up: cmd_port=", or `gpiod missing` in the read):

```bash
bash site_rollback.sh container
```

It does level 1 again, puts the old dpmg:latest tag back if the deploy rebuilt it, stops
dpmg_svc with `docker stop -t 30`, removes it, and starts it again from `dpmg:payload-pre-site-<date>`
with the old mounts, arguments and environment (`before/old_run_cmd.sh`, written by the before
read). Then the same read as level 1.

By hand, on the radio:

```bash
docker stop -t 30 dpmg_svc; docker rm dpmg_svc
# then paste the docker run line from before/old_run_cmd.sh on the laptop
docker logs --since 2m dpmg_svc 2>&1 | grep -E 'up: cmd_port=|Traceback'
```

Never `docker rm -f` a running dpmg_svc: a SIGKILL leaves the cards registered to the dead process
and every libsidekiq user reports "card in use" until the G40 reboots, and a G40 reboot puts the
HPA on with no drive for about 30 s (the shutdown pin is pulled low inside the 1189 until the
service takes it).

## If neither level brings the service up

- `docker logs --tail 80 dpmg_svc` and `docker ps -a` into the results folder.
- The older backups on the radio, newest first: `~/work/dpmg_backups/20261105` (the files the
  5 November deploy replaced, that is the 4 November versions, NOT the target),
  `20261104_215253`, `20261104_190358`, and `~/predeploy_20261103_100201.tgz` (the whole tree
  before 3 November). None of them is the pre-site state by itself; use them only to read a file.
- The payload flies with whatever service is up and answering, or not at all: the test lead's
  decision, written on the card.

## Time budget

| Step | Expected |
|---|---|
| level 1 copy and restart | 1 to 2 min (restart waits up to 60 s for a clean stop) |
| level 1 read back | under 1 min |
| level 2 stop, run, read | 2 min |
| G40 init on internal after either level, to show the radio works | 35 to 45 s (MAXN, 5 November) |
