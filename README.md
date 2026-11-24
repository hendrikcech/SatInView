# Trajectory-based Serving Satellite Identification with User Terminal's Field-of-View

This fork builds on the implementation for the LEO-NET'24 paper ***Trajectory-based Serving Satellite Identification with User Terminal's Field-of-View*** by [aliahan/SatInView](https://github.com/aliahan/SatInView).

It was updated for the SIGCOMM'26 paper "Dissecting the StarLink: Characterizing Queuing and Flow Dynamics in the Starlink Network".
See [Measure and process obstruction data](#measure-and-process-obstruction-data) for the simplified processing script.

Table of Contents
=================

* [Background](#background)
* [Prerequisites](#prerequisites)
* [Gather new data](#gather-new-data)
* [Measure and process obstruction data](#measure-and-process-obstruction-data)

## Background

Since the Starlink dish firmware update ([`286014e2-fea5-46e5-805f-85cd72450533.uterm_manifest.release`](https://github.com/clarkzjw/starlink-grpc-golang/commit/2979925ba64f82937c559b59673ae860b70edd7f)) in August 2024, a new `mapReferenceFrame` field was introduced to the output of the `dish_get_obstruction_map` gRPC call.

```go
type ObstructionMapReferenceFrame int32

const (
	ObstructionMapReferenceFrame_FRAME_UNKNOWN ObstructionMapReferenceFrame = 0
	ObstructionMapReferenceFrame_FRAME_EARTH   ObstructionMapReferenceFrame = 1
	ObstructionMapReferenceFrame_FRAME_UT      ObstructionMapReferenceFrame = 2
)

// Enum value maps for ObstructionMapReferenceFrame.
var (
	ObstructionMapReferenceFrame_name = map[int32]string{
		0: "FRAME_UNKNOWN",
		1: "FRAME_EARTH",
		2: "FRAME_UT",
	}
	ObstructionMapReferenceFrame_value = map[string]int32{
		"FRAME_UNKNOWN": 0,
		"FRAME_EARTH":   1,
		"FRAME_UT":      2,
	}
)
```

Example output from `grpcurl -plaintext -d {\"dish_get_obstruction_map\":{}} 192.168.100.1:9200 SpaceX.API.Device.Device/Handle`:

```
{
  "apiVersion": "26",
  "dishGetObstructionMap": {
    "numRows": 123,
    "numCols": 123,
    "snr": [
      -1,
...
      -1
    ],
    "minElevationDeg": 10,
    "maxThetaDeg": 80,
    "mapReferenceFrame": "FRAME_UT"
  }
}
```

Based on our measurement and observation, the `mapReferenceFrame` specifies the coordinate system used for the obstruction map data.

The possible values for `mapReferenceFrame` are:

- **FRAME_UNKNOWN (0)**: The reference frame is unspecified.
- **FRAME_EARTH (1)**: The obstruction map is aligned with the Earth and uses a fixed geographic coordinate system, with the top center pixel oriented toward the true north.
- **FRAME_UT (2)**: The obstruction map is aligned with the User Terminal, using a coordinate system relative to the dish's orientation. The bottom center pixel corresponds to the UT's boresight direction.


Our observation shows that most active Starlink dishes show `FRAME_EARTH`, most likely because they are in fixed, stationary locations and utilize a geographic coordinate system to track obstructions more efficiently, while dishes on `Roam`/`Mobile` or inactive dishes show `FRAME_UT`, partially because these dishes are often moved or relocated, requiring a coordinate system relative to the dish’s orientation to adapt dynamically to changing positions or conditions.

Thus, in addition to the method presented in the LEO-NET'24 paper, we also included the data processing notebook for UTs using `FRAME_EARTH`. 


## Prerequisites

### Prepare the environment

+ Clone the repository.

    ```bash
    git clone --recurse-submodules https://github.com/hendrikcech/SatInView.git
    cd SatInView
    ```

+ Create a Python virtual environment and install dependencies.

    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    pip install -r starlink-grpc-tools/requirements.txt
    ```

    **Note**: On a dual-homed machine connected to an inactive Starlink dish (i.e., without an active Starlink subscription), the Starlink router still advertises an IPv6 address to the local network. `Pip` would prefer to use IPv6 when installing packages, while an inactive dish cannot reach the Internet, which could halt the installation process. In this case, you can force `pip` to use IPv4 by setting the IPv4 addresses for the following domains in `/etc/hosts` or configure routing properly.

    ```
    151.101.0.223   pypi.org
    151.101.68.223  files.pythonhosted.org
    ```

+ Install `grpcurl` from https://github.com/fullstorydev/grpcurl to your `$PATH`.

### Set variables

1. Edit [`config.py`](./config.py), and set `IFCE` to the interface connected to Starlink. Set `DISH_ID` to the ID of the dish.
2. If IRTT tests are used, set `ENABLE_IRTT` to `True`, set `LOCAL_IP` to the local IP address for the Starlink interface `IFCE`, and set `IRTT_SERVER_PORT` to the IRTT server address and port.
3. The gateway of inactive Starlink dishes can be reached at `fe80::200:5eff:fe00:101`, which requires the stock Starlink router to be set in the `bypass` mode.
4. You may need to install the `iputils-ping` package instead of the `inetutils-ping` package on Debian-based systems to use the `-D` timestamp option.
```bash
apt list --installed | grep ping
```
5. You may also need to assign root privileges to the `ping` command to use the packet intervals less than 200ms or install [`iputils-ping>=20210722`](https://github.com/iputils/iputils/releases/tag/20210722), which [lowered the limit to 2ms](https://github.com/iputils/iputils/issues/317).

6. `main.py` starts gRPC status and the continuous obstruction worker immediately.
   Status is one snapshot per poll; use `--status-interval SECONDS` to set the
   positive polling interval (default: 60). Obstruction maps are polled immediately
   and about once per second; the map resets at UTC RI boundaries (seconds 12, 27,
   42, 57). Polling and reset detection can be delayed by a slow gRPC call. Output
   CSVs rotate by UTC hour and day without restarting the worker. TLEs are
   checked at startup and every six hours. The newest saved snapshot is reused if its
   download time (file modification time) is less than six hours ago; otherwise a new
   one is fetched.
   Failed TLE downloads are logged and retried at the next six-hour check without
   stopping collection. ICMP ping is commented out; IRTT remains opt-in.

### Dish GPS Location

You can get the GPS location of your dish by running the following command, which requires the `Allow access on local network` option to be enabled in the Starlink app at `Advanced` -> `Debug data` -> `Starlink location`.

```bash
grpcurl -plaintext -d {\"get_location\":{}} 192.168.100.1:9200 SpaceX.API.Device.Device/Handle
```

The GPS location is required to calculate and estimate the selected and serving satellites in post-processing.

## Gather new data

Run `main.py` to collect dish status and satellite trajectory data from the obstruction map:

```bash
.venv/bin/python main.py --results /path/to/results
```

`--results` overrides `DATA_DIR`, which otherwise defaults to `./data`.
Use an absolute path for unattended collection. Latency collection is disabled by default.
Status snapshots are collected every 60 seconds by default; configure the interval with
`--status-interval 120` to poll every two minutes.

By default, the data structure of the collected data is as follows:

```
data
├── grpc
│   └── 2024-10-08
│       └── GetStatus-2024-10-08-19-10-00.txt
├── latency
│   └── 2024-10-08
│       ├── irtt-10ms-5m-2024-10-08-19-10-00.json
│       └── ping-10ms-5m-2024-10-08-19-10-00.txt
└── TLE
    └── 2024-10-08
        ├── obstruction-data-<dish_id>-2024-10-08-19-10-01.csv
        └── starlink-tle-2024-10-08-19-10-00.txt
```

+ The `grpc` directory contains the dish diagnostic and alignment data from Starlink dish's gRPC interface (`192.168.100.1:9200`).
+ The `latency` directory contains the ICMP ping RTT measurement to Starlink's gateway. When IRTT test is enabled, the IRTT measurement data is stored in the same directory after each round finishes.
+ The `TLE` directory contains the collected obstruction and satellite trajectory data and the TLE snapshots of all Starlink satellites from Celestrak.
+ New obstruction CSV rows contain timestamp, Y, X, and the map reference frame. Older rows have only the first three columns; an empty frame means it was not reported. The Python processing scripts ignore the frame and do not automatically select a coordinate conversion.
+ The headerless CSV remains `Timestamp,Y,X,Frame` (three-column legacy files are accepted). Each successful poll writes all newly positive pixels with one shared timestamp/frame, or one row with blank Y/X when there are no new pixels; failed polls, held pixels, and disappearances produce no observation rows. At startup, the first map initializes the baseline; a successful reset instead establishes an empty baseline so new positives are recorded. Frame/shape changes reinitialize from the current map without reporting its accumulated pixels.
+ A poll can add multiple pixels, so capture is evidence rather than an ordered trajectory. The CLI and frame notebooks reject multiple coordinates with one timestamp instead of guessing their order. Multi-candidate track reconstruction and obstruction detection remain future work. The older frame notebooks also require whole-second timestamps; use the CLI for the fractional timestamps written by continuous capture.

## Measure and process obstruction data
1. Adjust `IFCE` (Starlink network interface) and `DISH_ID` (custom name) in `config.py`
2. Initialize the submodule, then capture data: `uv run --with-requirements requirements.txt --with-requirements starlink-grpc-tools/requirements.txt main.py --results ./data`
3. The relevant data will be saved to `./data/TLE/<current day>`.

    ``` bash
    $ ls data/TLE/2026-01-11
    obstruction-data-tum-2026-01-11-00-33-06.csv  starlink-tle-2026-01-11-00-33-00.txt
    obstruction-data-tum-2026-01-11-01-33-04.csv  starlink-tle-2026-01-11-01-33-00.txt
    obstruction-data-tum-2026-01-11-02-33-04.csv  starlink-tle-2026-01-11-02-33-00.txt
    obstruction-data-tum-2026-01-11-03-33-05.csv  starlink-tle-2026-01-11-03-33-00.txt
    ```

4. To infer which satellite the dish was connected, run `process_obstruction_data.py`. Check out its help message and set the correct location parameters (elevation, etc.). The program will parse all observation files in parallel.

    ``` bash
    $ uv run process_obstruction_data.py --tles data/TLE/2026-01-11/*txt --obs data/TLE/2026-01-11/*csv -o data/TLE/2026-01-11.csv
    ```

5. Check out the result.

    ``` bash
    $ head data/TLE/2026-01-11.csv
    Timestamp,Connected_Satellite,Generation,Distance,ObservedElevation,MatchedElevation,ObservedAzimuth,MatchedAzimuth
    2026-01-11 00:33:12+00:00,STARLINK-34913,v2mini,498.1555,75.5738,73.5725,100.3048,96.8861
    2026-01-11 00:33:13+00:00,STARLINK-34913,v2mini,499.7734,76.8412,72.9078,101.3099,95.212
    2026-01-11 00:33:14+00:00,STARLINK-34913,v2mini,501.486,74.3025,72.2346,99.4623,93.671
    2026-01-11 00:33:15+00:00,STARLINK-34913,v2mini,503.2922,74.3025,71.5548,99.4623,92.2498
    2026-01-11 00:33:16+00:00,STARLINK-34913,v2mini,505.1911,73.1763,70.8699,94.3987,90.9365
    2026-01-11 00:33:17+00:00,STARLINK-34913,v2mini,507.1817,73.2258,70.1813,90.0,89.7206
    2026-01-11 00:33:18+00:00,STARLINK-34913,v2mini,509.2628,71.9355,69.4903,90.0,88.5926
    2026-01-11 00:33:19+00:00,STARLINK-34913,v2mini,511.4333,71.9355,68.798,90.0,87.5442
    2026-01-11 00:33:20+00:00,STARLINK-34913,v2mini,513.6922,70.6452,68.1053,90.0,86.5678
    ```

## Tests

Run the unit tests and five-minute historical inference snapshot offline:

```bash
uv run --no-project --with pytest --with pandas --with numpy --with skyfield --with tqdm --with schedule --with grpcio --with yagrc python -m pytest -q tests
```

The snapshot takes about 40 seconds on the development machine. See
[`tests/fixtures/inference/README.md`](tests/fixtures/inference/README.md) for
the baseline and comparison tolerances.
