#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "skyfield",
#     "numpy",
#     "pandas",
#     "tqdm",
# ]
# ///

from skyfield.api import load, wgs84, utc
import pandas as pd
import numpy as np
from tqdm import tqdm

from datetime import datetime, timedelta, timezone
import math
import os
import argparse
import multiprocessing
from threading import Lock

# --- Process observed position and match sallites ---

# Calculate angular separation between two positions
def angular_separation(alt1, az1, alt2, az2):
    """Calculate the angular separation between two points on a sphere given by altitude and azimuth."""
    alt1, alt2 = np.radians(alt1), np.radians(alt2)
    az1 = (az1 + 360) % 360
    az2 = (az2 + 360) % 360
    az_diff = np.abs(az1 - az2)
    if az_diff > 180:
        az_diff = 360 - az_diff
    az_diff = np.radians(az_diff)
    separation = np.arccos(np.sin(alt1) * np.sin(alt2) + np.cos(alt1) * np.cos(alt2) * np.cos(az_diff))
    return np.degrees(separation)

# Calculate bearing (direction) between two points
def calculate_bearing(alt1, az1, alt2, az2):
    alt1, alt2 = np.radians(alt1), np.radians(alt2)
    az1, az2 = np.radians(az1), np.radians(az2)
    x = np.sin(az2 - az1) * np.cos(alt2)
    y = np.cos(alt1) * np.sin(alt2) - np.sin(alt1) * np.cos(alt2) * np.cos(az2 - az1)
    bearing = np.arctan2(x, y)
    bearing = np.degrees(bearing)
    return (bearing + 360) % 360

# Calculate bearing difference between two trajectories
def calculate_bearing_difference(observed_trajectory, satellite_trajectory):
    observed_bearing = calculate_bearing(observed_trajectory[0][0], observed_trajectory[0][1],
                                         observed_trajectory[-1][0], observed_trajectory[-1][1])
    satellite_bearing = calculate_bearing(satellite_trajectory[0][0], satellite_trajectory[0][1],
                                          satellite_trajectory[-1][0], satellite_trajectory[-1][1])
    bearing_diff = abs(observed_bearing - satellite_bearing)
    if bearing_diff > 180:
        bearing_diff = 360 - bearing_diff
    return bearing_diff

# Calculate the total angular separation and bearing difference
def calculate_total_difference(observed_positions, satellite_positions):
    total_angular_separation = 0
    for i in range(len(observed_positions)):
        obs_alt, obs_az = observed_positions[i]
        sat_alt, sat_az = satellite_positions[i]
        separation = angular_separation(obs_alt, obs_az, sat_alt, sat_az)
        total_angular_separation += separation
    bearing_diff = calculate_bearing_difference(observed_positions, satellite_positions)
    total_difference = total_angular_separation + bearing_diff
    return total_difference

def find_matching_satellites(satellites, observer_location, observed_positions):
    best_match = None
    closest_total_difference = float('inf')

    ts = load.timescale()

    for satellite in satellites:
        satellite_positions = []
        valid_positions = True

        for observed_time, observed_data in observed_positions:
            difference = satellite - observer_location
            topocentric = difference.at(ts.utc(observed_time.year, observed_time.month, observed_time.day, observed_time.hour, observed_time.minute, observed_time.second))
            alt, az, _ = topocentric.altaz()

            if alt.degrees <= 20:
                valid_positions = False
                break

            satellite_positions.append((alt.degrees, az.degrees))

        if valid_positions:
            total_difference = calculate_total_difference(
                [(90 - data[0], data[1]) for _, data in observed_positions],
                satellite_positions
            )
            if total_difference < closest_total_difference:
                closest_total_difference = total_difference
                best_match = (satellite, satellite_positions)

    return best_match, closest_total_difference

def calculate_distance_for_best_match(satellite, observer_location, start_time, interval_seconds):
    distances = []
    for second in range(0, interval_seconds + 1):
        current_time = start_time + timedelta(seconds=second)
        difference = satellite - observer_location
        topocentric = difference.at(current_time)
        distance = topocentric.distance().km
        distances.append(distance)
    return distances

# TODO: split data within RI by computing angular_separation between consecutive data points
# split if threshold is crossed
# -> find aperiodic reconfigurations

def get_observed_positions_sampled(df):
    if df.empty:
        print("No matching data found in merged_data_file.")
        return None
    if len(df) < 3:
        print("Not enough data points in merged_filtered_data.")
        return None
    start_data = df.iloc[0]
    middle_data = df.iloc[len(df)//2]
    end_data = df.iloc[-2]
    rotation = 0
    positions = [
        (start_data['Timestamp'], (90 - start_data['Elevation'], (start_data['Azimuth'] + rotation) % 360)),
        (middle_data['Timestamp'], (90 - middle_data['Elevation'], (middle_data['Azimuth'] + rotation) % 360)),
        (end_data['Timestamp'], (90 - end_data['Elevation'], (end_data['Azimuth'] + rotation) % 360))
    ]
    return positions

def get_observed_positions(df):
    if df.empty:
        print("No matching data found")
        return None
    rotation = 0
    positions = [(ts, (90 - ele, (az + rotation) % 360))
                 for ts, ele, az in zip(df.Timestamp, df.Elevation, df.Azimuth)]
    return positions

def process_second(df, current_time, observer_location, satellites):
    observed_positions, matching_satellite, matching_satellite_positions, match_error, distances = \
        None, None, [], None, []
    sky_time = load.timescale().from_datetime(current_time)

    observed_positions = get_observed_positions(df)
    if observed_positions is None:
        return observed_positions, matching_satellite, matching_satellite_positions, match_error, distances

    matching_satellite, match_error = find_matching_satellites(satellites, observer_location, observed_positions)
    if matching_satellite is None:
        return observed_positions, matching_satellite, matching_satellite_positions, match_error, distances

    matching_satellite, matching_satellite_positions = matching_satellite

    distances = calculate_distance_for_best_match(matching_satellite, observer_location, sky_time, 14)

    return observed_positions, matching_satellite, matching_satellite_positions, match_error, distances

def get_satellite_generation(sat):
    desg = sat.model.intldesg # e.g., 21040N -> launched 2021, 40th launch, sat N of launch
    launch = int(desg[:5])
    year = 2000 + int(desg[:2])
    num = int(desg[2:5])

    error_msg = f"Failed to get sat gen of {desg}"

    # Source: https://planet4589.org/space/con/star/stats.html
    # https://en.wikipedia.org/wiki/List_of_Starlink_and_Starshield_launches#Starlink_launches

    if year < 2018:
        print(error_msg)
        return None
    if launch == 18020:
        return "proto"
    if launch == 19029:
        return "v0.9"
    if launch >= 19074 and launch <= 21044:
        return "v1.0"
    if launch in [23026, 23056, 23067, 23079, 23096] or launch >= 23102:
        return "v2mini"
    # if launch >= 21059 and launch <= 23021:
    if launch >= 21059 and launch <= 23099:
        return "v1.5"

    print(error_msg)
    return None

def process_interval(args):
    current_time, duration_s, df, observer_location = args
    satellites = TLEManager.get(current_time)
    if satellites is None:
        print(f"Failed to find TLE for {current_time}")
        return None

    results = []

    df_end_time = current_time + pd.Timedelta(seconds=duration_s)
    df_lens = df[(df['Timestamp'] >= current_time) & ((df['Timestamp'] < df_end_time))]
    observed_positions, matching_satellite, matching_satellite_positions, match_error, distances = \
        process_second(df_lens, current_time, observer_location, satellites)
    if matching_satellite is not None:
        generation = get_satellite_generation(matching_satellite)
        # for second in range(duration_s+1):
        df_lens = df_lens.set_index("Timestamp")
        for i, ts in enumerate(pd.date_range(current_time, df_end_time, freq="s", inclusive="left")):
            result = dict(Timestamp=ts,
                           Connected_Satellite=matching_satellite.name,
                           Generation=generation)
            try:
                row = df_lens.loc[ts]
                result['Distance'] = round(distances[i], 4)
                result['ObservedElevation'] = round(row.Elevation, 4)
                result['MatchedElevation'] = round(matching_satellite_positions[i][0], 4)
                result['ObservedAzimuth'] = round(row.Azimuth, 4)
                result['MatchedAzimuth']= round(matching_satellite_positions[i][1], 4)
            except KeyError:
                pass
            results.append(result)

        print(f"{current_time} {duration_s} secs: {matching_satellite.name} ({generation}) error={match_error:.2f}")
    else:
        print(f"{current_time} {duration_s} secs: no matching satellite found")
    return results

def get_compute_intervals(df):
    df = df.set_index("Timestamp")
    current_start = df.index.min()
    duration = 1
    compute_timestamps = []
    # for ts, separation in zip(df.Timestamp.iloc[1:], df.Separation.iloc[1:]):
    for ts in pd.date_range(current_start + pd.Timedelta(seconds=1), df.index.max(), freq="s"):
        try:
            separation = df.loc[ts].Separation
        except KeyError:
            separation = 0
        if ts.second in [12, 27, 42, 57] or separation >= 3:
            compute_timestamps.append((current_start, duration))
            if duration != 15:
                print(f"Schedule aperiodic interval: {compute_timestamps[-1]}")
            current_start = ts
            duration = 1
        else:
            duration += 1
    compute_timestamps.append((current_start, duration))
    return compute_timestamps

# --- Process raw obstruction map and compute elevation and azimuth ---
def process_obstruction_data(filename):
    data = pd.read_csv(filename, sep=',', header=None, names=['Timestamp', 'Y', 'X'])
    data['Timestamp'] = pd.to_datetime(data['Timestamp'], utc=True)

    observer_x, observer_y = 62, 62  # Assume this is the observer's pixel location
    pixel_to_degrees = (80/62)  # Conversion factor from pixel to degrees

    positions = []
    for index, point in data.iterrows():
        dx, dy = point['X'] - observer_x, (123 - point['Y']) - observer_y
        radius = np.sqrt(dx**2 + dy**2) * pixel_to_degrees
        azimuth = np.degrees(np.arctan2(dx, dy))
        # Normalize the azimuth to ensure it's within 0 to 360 degrees
        azimuth = (azimuth + 360) % 360
        elevation = 90 - radius
        positions.append((point['Timestamp'], point['Y'], point['X'], elevation, azimuth))

    df_positions = pd.DataFrame(positions, columns=['Timestamp', 'Y', 'X', 'Elevation', 'Azimuth'])
    return df_positions

# ---

class TLEManager:
    tle_cache = dict()
    tles = []
    mutex = Lock()

    @staticmethod
    # looks for:
    # obstruction-data-tum-2025-11-27-08-21-03.csv
    def init(tle_paths):
        for tle_path in tle_paths:
            filename = os.path.basename(tle_path)
            if not os.path.isfile(tle_path) or not filename.startswith("starlink-tle"):
                print(f"Did not parse {tle_path}")
                continue
            parts = os.path.splitext(filename)[0].split("-")
            ts = datetime(*(int(v) for v in parts[2:]), tzinfo=timezone.utc)
            TLEManager.tles.append((ts, tle_path))
        TLEManager.tles = sorted(TLEManager.tles, key=lambda v: v[0])

    @staticmethod
    def get(ts):
        # self.tles are sorted by tle_ts
        best_tle_diff, best_tle_ts, best_tle_path = float("inf"), None, None
        for tle_ts, tle_path in TLEManager.tles:
            tle_diff = abs((ts - tle_ts).total_seconds())
            if tle_diff < best_tle_diff:
                best_tle_diff = tle_diff
                best_tle_ts = tle_ts
                best_tle_path = tle_path
            else:
                # tle_diff will only decrease until tle_ts > ts
                break
        if best_tle_path is None:
            return None
        if abs(best_tle_diff) > timedelta(hours=1).total_seconds():
            print(f"Matched TLE differs {best_tle_diff / 60 / 60:.2f} hours from ts {ts}")
        # print(f"Matched ts={ts} to TLE with ts={best_tle_ts}, diff={best_tle_diff / 60 / 60:.2f} hours")
        return TLEManager._load_tle(best_tle_path)

    @staticmethod
    def _load_tle(tle_path):
        with TLEManager.mutex:
            if tle_path not in TLEManager.tle_cache:
                TLEManager.tle_cache[tle_path] = load.tle_file(tle_path)
            return TLEManager.tle_cache[tle_path]


def find_jumps(df, threshold=3):
    # df = df.set_index("Timestamp")
    df["Elevation_shift"] = df.Elevation.shift()
    df["Azimuth_shift"] = df.Azimuth.shift()
    df["Separation"] = df.apply(lambda v: angular_separation(v.Elevation, v.Azimuth, v.Elevation_shift, v.Azimuth_shift), axis=1)
    diff_thresh = df[df.Separation > threshold]
    aperiodic = [ts for ts in diff_thresh.index if ts.second not in [12, 27, 42, 57]]
    return df.loc[aperiodic]

def compute_separation(df):
    df["Elevation_shift"] = df.Elevation.shift()
    df["Azimuth_shift"] = df.Azimuth.shift()
    df["Separation"] = df.apply(lambda v: angular_separation(v.Elevation, v.Azimuth, v.Elevation_shift, v.Azimuth_shift), axis=1)
    df.drop(columns=["Elevation_shift", "Azimuth_shift"], inplace=True)
    return df

def _worker_init(tle_paths):
    TLEManager.init(tle_paths)

def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--obs", nargs="+", required=True, help="csv log files obstruction-data-<name>-<date>.csv") #
    parser.add_argument("--tles", nargs="+", required=True, help="TLE files named starlink-tle-<year>-<month>-<day>-<hour>-<minute>-<second>.txt") #
    parser.add_argument("-o", required=True, help="Write results as csv to this file") #
    parser.add_argument("--lat", type=float, default=48.267548118257835) #
    parser.add_argument("--lon", type=float, default=11.668237289960858) #
    parser.add_argument("--ele", type=float, default=492) #
    parser.add_argument("--start", help="Start processing from this date") #
    parser.add_argument("--cpus", help="Start this number of parallel processes", default=multiprocessing.cpu_count(), type=int) #
    # parser.add_argument("-o", help="outdir", required=True) # obstruction-data-<name>-<date>.csv
    args = parser.parse_args()

    observer_location = wgs84.latlon(latitude_degrees=args.lat, longitude_degrees=args.lon, elevation_m=args.ele)

    # Only necessary for serial processing
    TLEManager.init(args.tles)

    with multiprocessing.Pool(args.cpus,
                              initializer=_worker_init,
                              initargs=(args.tles,)) as pool:
        results = []
        for log_i, log in enumerate(args.obs):
            print(f"[{log_i+1}/{len(args.obs)}] Processing {log}")
            df = process_obstruction_data(log)

            # Adds Separation column
            compute_separation(df)

            if args.start:
                df = df[df.Timestamp >= pd.to_datetime(args.start)]

            compute_timestamps = get_compute_intervals(df)

            map_args = [(ts, duration_s, df, observer_location) for (ts, duration_s) in compute_timestamps]
            results_nested = list(tqdm(pool.imap_unordered(process_interval, map_args), total=len(map_args), desc="parsing csvs"))
            # results_nested = list(tqdm(map(process_interval, map_args), total=len(map_args), desc="parsing csvs"))
            results.extend(x for xs in results_nested if xs is not None for x in xs)

            result_df = pd.DataFrame(results).sort_values("Timestamp")
            result_df.to_csv(args.o, index=False)
            print(f"Updated {args.o}")

if __name__ == "__main__":
    main_cli()
