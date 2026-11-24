# Trajectory-based Serving Satellite Identification with User Terminal's Field-of-View

This fork collects Starlink dish status, obstruction-map observations, and satellite orbital elements, then uses observed trajectories to infer the serving satellite.
It builds on the implementation for the LEO-NET'24 paper ***Trajectory-based Serving Satellite Identification with User Terminal's Field-of-View*** by [aliahan/SatInView](https://github.com/aliahan/SatInView).

It was updated for the SIGCOMM'26 paper "Dissecting the StarLink: Characterizing Queuing and Flow Dynamics in the Starlink Network".

## Contents

- [Setup](#setup)
- [Configure and collect](#configure-and-collect)
- [Output and reference frames](#output-and-reference-frames)
- [Infer satellites](#infer-satellites)
- [Optional latency measurements](#optional-latency-measurements)
- [Tests](#tests)

## Setup

Clone the repository with its `starlink-grpc-tools` submodule, which provides the Python obstruction-map client:

```bash
git clone --recurse-submodules https://github.com/hendrikcech/SatInView.git
cd SatInView
```

If you already cloned without submodules, run `git submodule update --init --recursive`.

Install [uv](https://docs.astral.sh/uv/) and set up the project environment (Python 3.13 or newer):

```bash
uv sync
```

Alternatively, create a virtual environment and run `pip install -r requirements.txt`.
Install `pytest` separately to run the tests with pip.
Keep `requirements.txt` in sync with the runtime dependencies in `pyproject.toml`.

Install [grpcurl](https://github.com/fullstorydev/grpcurl) on your `PATH` for dish status collection and the location command below.

On a dual-homed machine connected to an inactive dish, the Starlink router may advertise IPv6 without providing Internet access.
If package installation stalls, configure routing so that the package manager can reach the Internet.

## Configure and collect

Configure these values through environment variables or by editing `config.py`:

| Variable                  | Purpose                                                     | Default              |
|---------------------------|-------------------------------------------------------------|----------------------|
| `IFCE`                    | Interface connected to Starlink; used by optional ICMP ping | `eth0`               |
| `DISH_ID`                 | Name included in obstruction CSV filenames                  | `test`               |
| `DATA_DIR`                | Results directory                                           | `data`               |
| `STARLINK_GRPC_ADDR_PORT` | Dish gRPC address and port                                  | `192.168.100.1:9200` |

Run the collector:

```bash
IFCE=eth0 DISH_ID=my-dish uv run main.py --results /path/to/results
```

`--results` overrides `DATA_DIR`; use an absolute path for unattended collection.
Status snapshots are collected immediately and every 60 seconds by default; pass `--status-interval 120` to poll every two minutes.

The obstruction worker polls immediately and about once per second.
It resets the map at reconfiguration interval (RI) boundaries (seconds 12, 27, 42, and 57).
Slow gRPC calls can delay polling and reset detection.
CSV files rotate by hour and day without restarting the worker.

TLEs are checked at startup and every three hours.
The newest saved snapshot is reused if its file modification time is less than three hours old; otherwise a new one is downloaded. Failed TLE downloads are logged and retried at the next check without stopping collection. Latency collection is disabled by default.

## Output and reference frames

A typical results directory contains:

```text
data/
├── grpc/
│   └── 2026-01-11/
│       └── GetStatus-2026-01-11-00-33-00.txt
└── TLE/
    └── 2026-01-11/
        ├── obstruction-data-my-dish-2026-01-11-00-00-00.csv
        └── starlink-tle-2026-01-11-00-33-00.txt
```

`grpc/` contains dish status snapshots. `TLE/` contains obstruction observations and Celestrak TLE snapshots. Obstruction filenames use the UTC hour; TLE filenames use the download time.
Optional latency measurements are saved under `latency/`.

Obstruction CSVs have no header and use `Timestamp,Y,X,Frame`.
Timestamps are UTC and may include fractional seconds.
Legacy three-column files are accepted; an empty frame means it was not reported.
Each successful poll writes all newly positive pixels with a shared timestamp and frame, or one row with blank Y/X if there are no new pixels. Failed polls write no rows.

Since the August 2024 dish firmware update ([`286014e2-fea5-46e5-805f-85cd72450533.uterm_manifest.release`](https://github.com/clarkzjw/starlink-grpc-golang/commit/2979925ba64f82937c559b59673ae860b70edd7f)), `dish_get_obstruction_map` can report `mapReferenceFrame`:

- **FRAME_UNKNOWN (0)**: The reference frame is unspecified.
- **FRAME_EARTH (1)**: The map is Earth-aligned, with the top center toward true north.
- **FRAME_UT (2)**: The map is aligned with the user terminal, with the bottom center corresponding to its boresight direction.

We have observed `FRAME_EARTH` on most active dishes and `FRAME_UT` on some roaming, mobile, or inactive dishes.
The processor rejects `FRAME_UT` observations because it cannot convert them.
For missing or unknown frames, including legacy three-column files, it warns and assumes Earth-aligned coordinates.

## Infer satellites

The dish's GPS location is needed for inference.
To retrieve it, enable **Allow access on local network** in the Starlink app under **Advanced → Debug data → Starlink location**, then run:

```bash
grpcurl -plaintext -d '{"get_location":{}}' 192.168.100.1:9200 SpaceX.API.Device.Device/Handle
```

Run the processor with your dish's latitude and longitude in degrees and elevation in metres. Replace the example values with your own:

```bash
uv run process_obstruction_data.py \
  --tles data/TLE/2026-01-11/*txt \
  --obs data/TLE/2026-01-11/*csv \
  --lat 48.26 --lon 11.66 --ele 492 \
  -o data/TLE/2026-01-11.csv
```

The processor requires `--lat`, `--lon`, and `--ele`; it has no default observer location.
It selects the supplied TLE snapshot closest to each interval and warns if it differs by more than one hour; supply suitable snapshots even though the collector checks for new ones only every three hours.
Use `--cpus N` to limit parallel worker processes (the default is all available CPUs) and `--start DATE` to restrict processing to observations from that date.

The output CSV contains timestamps, inferred satellite names and generations, distances in kilometres, and observed and matched elevation and azimuth in degrees.
Matches are estimates, not confirmation of a connection.

## Optional latency measurements

IRTT is disabled by default.
To enable it, set `ENABLE_IRTT = True` in `config.py`, install the `irtt` executable, and configure `LOCAL_IP` and `IRTT_SERVER` (address and port) as environment variables or their corresponding values in `config.py`.
The current IRTT command forces IPv6 and requires a reachable IRTT server.
It runs hourly, with the interval and duration controlled by `INTERVAL` and `DURATION` (defaults: `10ms` and `60m`).

ICMP ping is commented out in `main.py` and does not run by default.
If enabled in the code, it uses `IFCE` and `STARLINK_DEFAULT_GW`.
On Debian-based systems, `iputils-ping` may be needed for the `-D` timestamp option; sub-200 ms intervals may require privileges or [`iputils-ping>=20210722`](https://github.com/iputils/iputils/releases/tag/20210722).

## Tests

Run the unit tests and five-minute historical inference snapshot offline:

```bash
uv run pytest -q
```

The snapshot takes about 40 seconds on the development machine. See [`tests/fixtures/inference/README.md`](tests/fixtures/inference/README.md) for the baseline and comparison tolerances.
