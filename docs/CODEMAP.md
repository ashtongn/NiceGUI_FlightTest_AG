# DPMG code map

This map says what each file in the tree does, folder by folder. Two roles
share one codebase. The GUI runs on the x86 in the container `dpmg`. The radio
service runs on each radio's Jetson in the container `dpmg_svc`, with the tree
bind mounted at `/app_svc`.

All importable code is in the `dpmg/` package. The repo root keeps only the
entry points, the launchers, the image and dependency files, and the radio
state files that the code finds by location: `rf_frontend.yaml`,
`payload_profiles.yaml`, `requirements-crypto.txt`, `output/`,
`docs/datasheets`, `scenarios/json` and `mothball_fury.svg`. `dpmg/paths.py`
holds those locations.

Import by package name (`from dpmg.radio import sdr_core`) and run modules
with `python3 -m` from the repo root (`python3 -m dpmg.scenario.gen_scenario_json`).
Flat names (`import sdr_core`) do not import. `tools/reorg/moves.tsv` lists
the old and new path of each module. `dpmg_service.py` stays at the root as the service entry.

## Repo root
| File | Purpose |
|---|---|
| `dpmg_service.py` | The radio service entry. Every radio container runs `python dpmg_service.py --kind ...` from the root of the mounted tree, and the code itself is in `dpmg/radio/dpmg_service.py`. |
| `run_gui_link.py` | Runs the GUI on a chosen port, as `run_gui_link.py [port] [discovery_port]`. The second argument lets it run beside the deployed container, which holds UDP 47811. `launch_gui.sh --native` and the Windows launchers use it. |
| `deploy.sh` | Runs from the x86. It backs up and pushes code to a radio, rebuilds its image, brings up `dpmg_svc` through `deploy/radio/`, then starts the GUI. `--frontend` sets up the G40 GPIO front end. |
| `update.sh` | Fast code only update. It rsyncs the changed files to the radio and restarts `dpmg_svc`. |
| `launch_gui.sh` | Brings up the GUI container `dpmg` on the x86. It syncs the tree into `/app`, runs an import self-check, and restarts the container. |
| `start_console.bat`, `launch_gui.bat`, `launch_gui.ps1` | The Windows console. They run the GUI natively through `run_gui_link.py`. |
| `Dockerfile` | Builds the `dpmg` image, used for the GUI on the x86 (`python -m dpmg.web.nicegui_app`) and for the radio service on the G40 and X40. |
| `requirements*.txt`, `.env.example` | The Python dependencies (GUI, radio, crypto, dev, Windows) and the template for deployment settings. |
| `payload_profiles.yaml` | Payload RF chain profiles (HPAs, switch, LNA, attenuators) for the payload view and the guard. |
| `rf_frontend.example.yaml` | An example front end config. The radio's own `rf_frontend.yaml` sits beside it. |

## `dpmg/radio/`: radio service and hardware
| File | Purpose |
|---|---|
| `dpmg_service.py` | The headless radio service. It runs the UDP command and telemetry plane, the scheduler, detection, decode, tools, the front end and the bitpipe modem. |
| `sdr_core.py` | The SDR backends, pipeline, scheduler and analysis, with no GUI framework: EpiqBackend, MockBackend, the USB radio backends used by `../NiceGui_SuperNano`, and `make_backend`. |
| `skiq_bridge.py` | The ctypes bridge to libsidekiq (Epiq X40 and G40/NV100). It loads the native RX drain from `native/`. |
| `radio_role.py` | Says what each radio is acting as: receiver, jammer, playback, transponder, bitpipe modem, dCFE node or engineering. |
| `fpga_image.py` | Reports which FPGA image a radio runs and loads another one. |
| `host_thermal.py` | Reads the radio host's on-chip temperature for the status heartbeat. |
| `card_imu.py` | Reads tilt, shock and motion from the NV100 on-card accelerometer. |
| `gps_antenna.py` | Controls the G40 GPS antenna 3.3 V bias and reports the GNSS sky status. |
| `g40_clock.py` | Reads the G40's external 10 MHz and 1 PPS state from the GPS module (the `epiq-gps-ref` setting and which source disciplines the carrier clock). |
| `barometer.py` | Reads the BMP581 or BMP585 barometer on the radio host's I2C bus for the status heartbeat. |
| `port_map.py` | The port map: the `ports:` section of `rf_frontend.yaml`, which says what each SMA jack may be used for, and the rules the service, the GUI and the planner share. |
| `iq_record.py` | Records a stretch of live RX IQ and rebuilds it for TX replay. |

## `dpmg/link/`: wire protocol and the GUI side of the link
| File | Purpose |
|---|---|
| `proto.py` | The DPMG wire protocol (version 3): message types, encode and decode. |
| `link_client.py` | The GUI side UDP link client: discovery, one HostLink per serial, and the link sessions opened by HELLO. |
| `view_sync.py` | Keeps the operator console and the engineering view showing the radio's real settings. |
| `ground_log.py` | Writes the Log information files on the laptop, per radio, from its own task so logging outlasts any page; one set of files per start. See `docs/LOGGING.md`. |
| `acft_pos_multicast.py` | Reads aircraft position and altitude from a UDP multicast feed (NMEA or JSON). `python3 -m dpmg.link.acft_pos_multicast send nmea` is a test sender. |

## `dpmg/dsp/`: detection, channelizer, decode, analysis
| File | Purpose |
|---|---|
| `channelizer_worker.py` | Runs the GPU channelizer and the demodulators on the live SDR stream. |
| `channelizer_client.py` | The client side of the GPU channelizer service, over POSIX shared memory. |
| `energy_detector.py` | The wideband energy detector and its fixed grid slot mapping. |
| `channels.py` | The channel configuration model for the channelizer. |
| `decode_pool.py` | A bounded decode pool per channel, using spawned processes, or threads as a fallback. |
| `decode_log.py` | The live log of decode events. |
| `demod_helper.py` | A demodulator wrapper that adds carrier recovery and a symbol timing search. |
| `psd_trace.py` | Computes the PSD and records traces for the RX path. |
| `burst_history.py` | The history of burst events (DETECTED, DECODED, LOST). |
| `region_extract.py` | Cuts a frequency band out of a wideband IQ capture. |
| `tools_engine.py` | Test signal generators (chirp, tone, two-tone, noise), IQ measurements and `detect_signals`. |
| `loopback_source.py` | A synthetic RX frame source for the channelizer worker. |
| `amc.py` | Blind automatic modulation classification of a captured IQ slice. |
| `cdif_reader.py` | Reads `.cdif` IQ recordings (Tektronix SignalVu, SCEPTRE). |
| `iq_resample.py` | Loads, resamples and saves IQ in the supported file formats. |

## `dpmg/gpu/`: GPU channelizer container
The `dpmg-gpu` image (`deploy/docker/Dockerfile.gpu*`) copies these two files
flat into `/app`, and `gpu_service.py` imports `gpu_channelizer2` by its plain
name. Nothing else in `dpmg/` imports them.

| File | Purpose |
|---|---|
| `gpu_service.py` | The GPU channelizer service, run in the dpmg-gpu container. |
| `gpu_channelizer2.py` | The batched CuPy channelizer: FFT, slice, IFFT and slot power. |

## `dpmg/framing/`: frames and CRC
| File | Purpose |
|---|---|
| `frame_codec.py` | Framing with sync, length, payload and CRC (encode, find, decode), plus the FEC and secure layouts. |
| `frame_spec.py` | A declarative frame descriptor, used to build frames (author) and parse them (ingest). |
| `frame_integration.py` | Connects `frame_spec` to the app UI and the channelizer. |
| `crc_util.py` | A parameterized CRC engine with named presets. |

## `dpmg/modem/`: packet modem
| File | Purpose |
|---|---|
| `modem.py` | A half duplex packet modem built on the waveform factories and `frame_codec` framing. |
| `transceive.py` | The modem's reactive loop: receive, decode, then transmit a response. |

## `dpmg/modulation/`, `dpmg/demodulation/`: waveforms
| File | Purpose |
|---|---|
| `modulation/modulator.py` | `get_modulator`, which dispatches FSK, 4FSK, PSK, GFSK, DSSS, CSS and the higher order modulations. |
| `modulation/higher_order.py` | 8PSK, 8QAM, 16QAM, 16APSK and 32APSK, with soft LLR output. |
| `modulation/spread_spectrum.py` | DSSS with one or two codes over BPSK, QPSK, OQPSK or pi/4-QPSK, and CSS/LoRa. |
| `modulation/lfsr_codes.py` | General LFSR and Gold spreading codes. |
| `modulation/spreading_codes.py` | The DSSS code resolver for the seed, m-sequence, Gold, custom and LFSR families. |
| `demodulation/demodulator.py` | Soft decision demodulators (FSK, 4FSK, QPSK, GFSK) and the higher order adapter. |
| `demodulation/channel_simulator.py` | An AWGN channel simulator. |

## `dpmg/scenario/`: scenarios and channel plans
| File | Purpose |
|---|---|
| `gen_scenario_json.py` | Builds test scenarios from JSON (`python3 -m dpmg.scenario.gen_scenario_json spec.json`, `--presets`, `--dsss-presets`). |
| `channel_plan.py` | The scenario and channel plan schema for several waveforms, shared by TX compose and the RX channelizer. |
| `plan_adapter.py` | Converts the plan file format to `channels.ChannelConfig`. |
| `plan_compose.py` | Renders a loaded plan into one wideband IQ block for the loopback self-test. |
| `dsss_builder_validate.py` | The input checks for the DSSS builder. |
| `dsss_replay.py` | Offline DSSS replay through a G40-like channel and the GPU slicing model. |

## `dpmg/payload/`: front end and payload chain
| File | Purpose |
|---|---|
| `rf_frontend.py` | The external RF front end on the G40: PA enable, TX/RX switch, PA alarm and TR readback. It loads `rf_frontend.yaml` from the root. |
| `frontend_setup.py` | Builds, validates and reports `rf_frontend.yaml` (`deploy.sh --frontend`; `python3 -m dpmg.payload.frontend_setup pins`). |
| `mc_atten.py` | Drives the Mini-Circuits USB attenuator (RCDAT-6G-120H family). |
| `payload_budget.py` | The payload RF chain math for the payload view, the guard and the workbook. It loads `payload_profiles.yaml` from the root. |
| `payload_guard.py` | The RX front end protection interlock, checked every time the PA keys. |
| `device_watch.py` | Plug-in detection for the USB attenuator and the switch readback ADC while the front end is open. |

## `dpmg/secure/`: secure messages
| File | Purpose |
|---|---|
| `api.py` | One import for secure messages (`from dpmg.secure import api as sf`) that re-exports the pieces below. |
| `secure_frame.py` | Seals and opens DPMG frames. |
| `keys.py`, `nonce.py`, `replay.py` | The key file and key derivation, nonce state that never repeats, and anti-replay on receive. |
| `crypto_env.py` | Checks the crypto stack at start-up against the pin in `requirements-crypto.txt` at the root. |
| `transec_sync.py` | A keyed 32 bit sync word (simulator hook). |
| `pq_rekey.py` | Interface stubs for a post-quantum rekey. |
| `secure_ground.py` | The ground station side. It opens sealed frames that the radios report. |

## `dpmg/web/`: NiceGUI app and tabs
| File | Purpose |
|---|---|
| `nicegui_app.py` | The NiceGUI app: the operator console, the engineering view (Encoder, IQ Validator, MXG and SDR tabs; the ACM tab is present but disabled, marker `ACM_MOTHBALL`), and the `/mission` and `/bitpipe` pages. |
| `link_tab.py` | The ground link SDR tab: remote radios, the Tools panel, Connect by IP and saved radios (`output/saved_radios.json`). |
| `channelizer_tab.py` | The Channelizer tab. |
| `scenario_tab.py` | The Scenario Builder section inside the Encoder tab. |
| `plan_ui.py` | Loads a plan `.json` and pushes it to the worker as the watch list. |
| `history_ui.py` | The burst history panel, with CSV export. |
| `survey_ui.py` | The blind survey panel for the SDR tab. |
| `dsss_builder_tab.py` | The DSSS builder panel in the engineering SDR tab. |
| `role_fpga_ui.py` | The radio role and FPGA image panels. |
| `payload_tab.py` | The payload view in the engineering view. It serves `docs/datasheets`. |
| `port_pick_ui.py` | The GUI side of the port map: greyed out jacks and chain tooltips in the operator console, the engineering SDR tab and the mission step editor, and the lockout panel. |
| `timer_patch.py` | Lets a `ui.timer` on a deleted page end quietly. Imported once by `nicegui_app`. |

## `dpmg/mission/`: mission planner (`/mission`)
| File | Purpose |
|---|---|
| `mission_tab.py` | The mission view: catalog, timeline lanes, step editor, map, run controls and audit log. |
| `catalog.py` | The technique catalog: jammers, tools, record and replay, playback, transponder and setup. |
| `mission_file.py` | The dpmg-mission plan file: new, load, dump and validate. |
| `mission_runner.py` | Turns steps into service commands keyed on the clock, and keeps the audit log. |
| `mission_import.py` | Importers for GPX, KML and CFE CSV or XML. |
| `triggers.py`, `position_feed.py`, `motion_sim.py` | Location triggers, the platform position source and simulated motion. |
| `map_element.py`, `static/mission_map.js`, `map_store.py`, `base_map.py`, `map_import.py` | The offline MapLibre map, the PMTiles store under ~/.dpmg/maps, the base map, and import of dropped map files. |
| `ports.py`, `payload_sweep.py`, `scenario_spec.py` | Port choice per step, the payload checkout sweep, and scenario JSON helpers. |

## `dpmg/bitpipe/`: streamed link (`/bitpipe`)
| File | Purpose |
|---|---|
| `engine.py` | `LinkEngine`: stream mux, selective repeat ARQ, seal and open, one frame per FEC block, sync and deframer. It has no sockets. |
| `container.py` | `BitpipeContainer`: one TCP port per stream on the application side and one bit stream on the modem side. |
| `config.py`, `frame.py`, `coding.py`, `crypto.py`, `deframer.py`, `arq.py`, `streams.py`, `transport.py` | The pieces of the pipe, described in docs/BITPIPE.md. |
| `stream_modem.py`, `modem_link.py` | The continuous modem and the pull client on the service side (`bitpipe_start`). |
| `loopback.py`, `sim_link.py` | The whole link in software, without and with IQ channels. |
| `peer.py`, `link_panel.py`, `ctl_client.py` | The laptop peer (`python3 -m dpmg.bitpipe.peer`), the NiceGUI link panel and the control clients. |

## `dpmg/fec/`, `dpmg/encoding/`, `dpmg/services/`, `dpmg/ui/`
| File | Purpose |
|---|---|
| `fec/conv.py`, `rs.py`, `ldpc.py`, `tpc.py`, `acm.py`, `channel.py` | FEC tiers 1 to 5 (Viterbi, RS with interleaving, QC-LDPC, TPC, ACM) and the shared BER channel. |
| `encoding/ecc_decoder.py`, `ecc_decode.py`, `dmr_template.py` | DMR Hamming, Golay and BPTC encode and decode, and the DMR reference template. |
| `services/encoder_service.py`, `validator_service.py` | Build bit strings and waveforms; parse IQ files and compare bits. |
| `ui/fsk_encoder.py`, `ui/data_validation.py` | Helpers for the FSK encoder and the DMR validator. |

## Other files in `dpmg/`
| File | Purpose |
|---|---|
| `paths.py` | Defines `REPO_ROOT`, the search order for native libraries (`native/lib/`, then the root, then beside the module), and gives files below `output/` to the folder's owner. |
| `deploy_config.py` | Host, port and storage settings in one place, each one overridable from the environment or `.env`. |
| `rec_state.py` | Tracks which radios are recording, as this GUI process sees it. |
| `run_notice.py` | What a mission run is waiting on (a radio initializing), for the banner every GUI page shows. |

## `native/`: C helpers loaded with ctypes
| File | Purpose |
|---|---|
| `rxdrain.c` | The native RX batch drain for the Epiq backend. Its header has the details. |
| `build.sh` | Builds `native/lib/librxdrain.so`. On the radio, run it inside the dpmg image. |
| `lib/` | Built libraries, not in git. `skiq_bridge.load_rxdrain` also accepts the old `librxdrain.so` at the root. |

## `deploy/`
| Path | Purpose |
|---|---|
| `radio/g40_up_r36.sh` | Starts the G40 (JetPack 6) `dpmg-gpu` and `dpmg_svc` containers with TX, without prompts or sudo. `APP` is the code folder. |
| `radio/dpmg_up.sh` | Guided radio bring-up. It prompts for kind, serial, addresses, ports and TX, and saves `.env` at the repo root. |
| `radio/dpmg_doctor.sh` | Diagnoses and recovers the JetPack 5 radio stack: syntax check, stale card lock and IPC, rebuild, restart and verify. |
| `radio/x40_tx_up.sh` | Starts the X40 service container with the RT budget in one step. |
| `docker/Dockerfile.gpu`, `docker/Dockerfile.gpu.r36` | The GPU channelizer images for JetPack 5 and JetPack 6. The build context is the repo root. |
| `push_update.sh` | Copies named files into the running G40 service and the GUI container, with backups and hash checks. |
| `g40/` | G40 extras kept on the laptop: the udev rule, the front end Dockerfile overlay, the driver config, the GPS bias unit and the guarded flash script. |
| `ntp/`, `wheels/` | Clock sync setup, and offline wheels that the Dockerfile installs. |

## `bench/`: hardware drivers and checkouts
| File | Purpose |
|---|---|
| `bench_transceive.py` | The main hardware CLI. It is a direct proto client that inits, streams and keys the radios (commands such as gen-test, checkout, chan-test and transponder): `python3 bench/bench_transceive.py --host <ip> ...`. |
| `first_checkout.py` | The scripted first checkout. It runs the sibling scripts (`mod_fec_matrix.py`, `snr_sweep.py`, `port_matrix.py` and others) by path. |
| `checkout_common.py`, `fsk_loopback_test.py`, `bench_modsweep.py` | Shared helpers and case libraries, also imported by tests. |

## `tests/`
Offline tests, one file per area. `tests/run_all.sh` runs all of them, with
pytest for files written for it and as a plain script otherwise (exit 0 is a
pass), and writes a results table. To run one file from the repo root:
`python3 tests/test_x.py`. Five tests guard the layout itself:

- `test_import_graph.py`: every import resolves;
- `test_runtime_paths.py`: files found by location stay at the root;
- `test_path_refs.py`: every path a script names exists;
- `test_decode_pool_spawn.py`: spawned decode workers load the parent's files;
- `test_entry_points.py`: the service entry runs the way the containers run it.

## `tools/`
| File | Purpose |
|---|---|
| `gen_flight_scenarios.py` | Writes the flight test scenario JSONs (X40 and G40 pairs). |
| `gen_payload_workbook.py` | Writes the payload EIRP, RX protection and link budget workbook from `payload_profiles.yaml`. |
| `gen_hpa_bench_mission.py`, `gen_hpa_bench_sheet.py` | Write a timed mission plan and a fill-in workbook for the HPA bench power sweep. |
| `build_scenarios.py` | Writes the hard coded flight test scenario library into `scenarios/`. |
| `amc_eval.py`, `amc_scenarios.py` | Offline validation of the AMC classifier, and multi-signal AMC scenarios. |
| `decode_probe.py` | A read only probe that runs the real worker decode on a channel dump. |
| `tx_health_check.py` | The X40 TX health check: clean emitters, or mush and replicas. |
| `sim_position.py`, `maps/` | A test position player, and the builders for the offline map assets and base map. |
| `md_to_docx.py` | Converts Markdown to .docx. |
| `reorg/` | Folder move helpers. `moves.tsv` holds the old and new path of every moved module, and `launch_gui.sh` reads it to clear old root copies from the GUI container. `move.py` moves a group and leaves a forwarding file, `codemod.py` rewrites flat imports to package imports, and `docpaths.py` updates paths in docs. |

## Docs and data
- `README.md`: the folder map and everyday commands.
- `docs/*.md`: guides and this map.
- `docs/datasheets/`.
- `scenarios/`: curated scenario specs in `scenarios/json/`; also a root for
  the radio file browser.
- `data/` and `output/` are made at run time and are not in git.
