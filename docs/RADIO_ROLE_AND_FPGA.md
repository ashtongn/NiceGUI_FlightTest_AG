# Radio role and FPGA image

## Radio role

Each radio has a role, which says what it is acting as. The service keeps the role on
the radio host, in `data/role/<serial>.json` under its data folder. So the role survives
a service restart, and every GUI and script sees the same one. The roles are defined in
`dpmg/radio/radio_role.py`.

| role                | panels on the operator console               | the radio refuses                                   |
|---------------------|----------------------------------------------|-----------------------------------------------------|
| Any (engineering)   | everything                                   | nothing                                             |
| Receiver / monitor  | tuning, receive, record, detections          | anything that transmits                             |
| Jammer              | tuning, receive, transmit, jammer, detections | bitpipe, transponder, transceive                    |
| IQ playback         | tuning, receive, transmit, IQ playback, record, detections | bitpipe, transponder, transceive       |
| Transponder         | tuning, receive, transmit, transponder, detections | bitpipe                                       |
| Bitpipe modem       | tuning, receive, transmit, link to /bitpipe  | waveform loads, transponder, transceive, replay, ground checkout; start_tx only with the stream modem attached |
| dCFE node           | none (status only)                           | everything that opens or streams the radio          |

Stops (stop_tx, stop_rx, close), status and the role and FPGA commands pass in every
role. A role never loosens a TX limit: the same-card, payload guard and rate checks still
apply.

The dCFE node role is still accepted by the code, so a role file that names it loads,
but dCFE is not in this repo and the role is not used.

To set the role:

- Operator console: there is no dropdown. The card shows `Role: ...`, and the detail
  panel follows the role the radio reports.
- Engineering SDR tab: the Radio role dropdown at the top of the control card.
- Script: CMD `set_role {"role": "monitor", "by": "bench"}`. `get_config` reports
  `role`.
- Service start: `--role monitor` sets the role only when none is stored yet.

The service refuses a role change while TX is running or an FPGA load runs. dCFE node
needs the radio closed first. While the bitpipe modem is attached, the role can only
change to Any or Bitpipe; stop the modem first to pick another.

Mission planner: each plan radio has a role dropdown. When it is set, the validator flags
steps that role does not run. The dropdown shows the role the connected radio reports,
and a note when the two differ.

## FPGA image

Engineering SDR tab, FPGA image panel: one row per radio with the image it runs, then the
controls for the selected radio.

- Ettus X410: the image is stored on the radio. The panel lists the images in the images
  folder on the radio host (`DPMG_UHD_IMAGES`, else `UHD_IMAGES_DIR`, else
  `~/x410_uhd411`). Each image needs its `.bin` and `.dts`. Load runs
  `uhd_image_loader --args <DPMG_UHD_ARGS>,fpga=<image>`, and the radio drops off the
  network while it loads. The service first stops RX and closes the radio. Afterwards it
  waits for the radio to come back, checks that `uhd_find_devices` reports the new image,
  and runs `uhd_usrp_probe`. The radio is left closed; start it again afterwards. Read
  back runs the probe on demand (radio closed) or reads the property tree (radio open).
  The X410's personality follows the image. Its caps (rates, master clock, channels) come
  from a per-image table in `dpmg/radio/fpga_image.py` and are read again after a load.
  Initialize is refused for an image with no table, for an image that changed since the
  GUI built its controls, and for a rate the 1 GbE link cannot carry. The table and
  measurements are in docs/X410.md of the Super Nano tree.
- Ettus B200 family: nothing is stored on the radio, and UHD loads an image at every
  open. Load picks the file UHD opens with (`fpga=` in the device args) and stores it in
  `data/fpga/<serial>.json`. An open radio is reopened with it; a closed one uses it at
  the next start. "UHD default" removes it.
- Epiq G40 and X40: read only. The panel shows the FPGA version, git hash and build date
  per card, read from libsidekiq while the radio is started. NV100 flashing has its own
  procedure (golden fallback, one card at a time) and is not done from the GUI.

Every load goes through a confirm dialog that names the radio and the current and new
image. The service refuses a load while TX runs, without the image name repeated as
`confirm`, for an image not on the radio host, and while another load runs. During a
load the radio refuses everything except stops, status and the other read only commands
that pass in every role.

Commands: `fpga_status {"probe": bool}` and `fpga_load {"image": X, "confirm": X}`.
`get_config` carries a short `fpga` entry (image, version, load step).

Tests: `tests/test_radio_role.py`, `tests/test_fpga_image.py`,
`tests/test_role_fpga_ui.py`.
