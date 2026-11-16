#!/usr/bin/env python3

import csv
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from skyfield.api import load

from config import STARLINK_GRPC_ADDR_PORT, TLE_URL, TLE_DATA_DIR, DISH_ID
from util import date_time_string, ensure_directory, ensure_data_directory

sys.path.insert(0, str(Path(__file__).resolve().parent / 'starlink-grpc-tools'))
import starlink_grpc


satellites = None

logger = logging.getLogger(__name__)


def capture_snr_data(duration_seconds, interval_seconds, context, stop_event=None):
    snapshots = []
    end_time = time.monotonic() + duration_seconds

    while time.monotonic() < end_time and (stop_event is None or not stop_event.is_set()):
        try:
            snr_data = starlink_grpc.obstruction_map(context)
            snr_data_array = np.array(snr_data, dtype=float)
            snr_data_array = (snr_data_array > 0).astype(int)
            snr_data_array[snr_data_array == -1] = 0
            snapshots.append(snr_data_array)
        except starlink_grpc.GrpcError as e:
            logger.warning("Failed getting obstruction map data: %s", e)
            break
        if stop_event is not None:
            if stop_event.wait(interval_seconds):
                break
        else:
            time.sleep(interval_seconds)

    return snapshots


def save_white_pixel_coordinates_xor(directory, snapshots, start_time):
    previous_snr_data = np.zeros_like(snapshots[0])
    hold_coord = None

    for i, snr_data in enumerate(snapshots):
        xor_snr_data = np.bitwise_xor(previous_snr_data, snr_data)
        coords = np.argwhere(xor_snr_data == 1)

        if coords.size > 0:
            coord = coords[-1]
            hold_coord = coord
        elif hold_coord is not None:
            coord = hold_coord
        else:
            continue

        timestamp = start_time + timedelta(seconds=i)
        day_directory = Path(directory) / timestamp.strftime("%Y-%m-%d")
        ensure_directory(str(day_directory))
        filename = "obstruction-data-{}-{}.csv".format(
            DISH_ID, timestamp.strftime("%Y-%m-%d-%H-00-00"))
        with open(day_directory / filename, 'a', newline='') as csvfile:
            csv.writer(csvfile).writerow([
                timestamp.strftime("%Y-%m-%d %H:%M:%S"), coord[0], coord[1]])
        previous_snr_data = snr_data


def wait_until_target_time(stop_event=None, last_target=None):
    target_seconds = {12, 27, 42, 57}
    while stop_event is None or not stop_event.is_set():
        target = datetime.now(timezone.utc).replace(microsecond=0)
        if target.second in target_seconds and target != last_target:
            return target
        if stop_event is not None:
            if stop_event.wait(0.5):
                return None
        else:
            time.sleep(0.5)
    return None


def load_satellites():
    global satellites
    try:
        snapshots = Path(TLE_DATA_DIR).glob("*/starlink-tle-*.txt")
        latest = max(snapshots, key=lambda path: path.stat().st_mtime, default=None)
        if latest is not None and time.time() - latest.stat().st_mtime < 6 * 3600:
            satellites = load.tle_file(str(latest))
        else:
            directory = Path(TLE_DATA_DIR).joinpath(ensure_data_directory(TLE_DATA_DIR))
            satellites = load.tle_file(TLE_URL, True, str(directory / f"starlink-tle-{date_time_string()}.txt"))
        logger.info("Loaded %s Starlink satellites", len(satellites))
    except OSError:
        logger.exception("Failed loading TLEs; collection will continue and retry at the next TLE job")


def collect_obstruction_data(stop_event=None):
    if stop_event is None:
        stop_event = threading.Event()

    context = starlink_grpc.ChannelContext(target=STARLINK_GRPC_ADDR_PORT)
    timeslot_duration_seconds = 14
    interval_seconds = 1  # Capture a snapshot every 1 second
    round = 0
    last_target = None

    try:
        while not stop_event.is_set():
            target = wait_until_target_time(stop_event, last_target)
            if target is None or stop_event.is_set():
                break
            last_target = target

            round += 1
            logger.info("Current round %s", round)
            try:
                starlink_grpc.reset_obstruction_map(context)
                start_time = datetime.now(timezone.utc)
                snapshots = capture_snr_data(
                    timeslot_duration_seconds, interval_seconds, context, stop_event)
            except starlink_grpc.GrpcError:
                logger.exception("Failed collecting obstruction data; retrying next timeslot")
                continue

            if snapshots:
                save_white_pixel_coordinates_xor(TLE_DATA_DIR, snapshots, start_time)
            else:
                logger.warning("No obstruction snapshots captured; retrying next timeslot")
    finally:
        context.close()
