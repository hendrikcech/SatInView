#!/usr/bin/env python3

import csv
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import grpc
from skyfield.api import load

from config import STARLINK_GRPC_ADDR_PORT, TLE_URL, TLE_DATA_DIR, DISH_ID
from util import date_time_string, ensure_directory, ensure_data_directory

sys.path.insert(0, str(Path(__file__).resolve().parent / 'starlink-grpc-tools'))
import starlink_grpc


satellites = None

logger = logging.getLogger(__name__)


def get_obstruction_snapshot(context):
    try:
        map_data = starlink_grpc.get_obstruction_map(context)
        timestamp = datetime.now(timezone.utc)
        snr_data = np.array(map_data.snr, dtype=float).reshape(
            map_data.num_rows, map_data.num_cols)
        snr_data = (snr_data > 0).astype(int)
        field = map_data.DESCRIPTOR.fields_by_name.get('map_reference_frame')
        value = getattr(map_data, 'map_reference_frame', None)
        enum = field.enum_type.values_by_number.get(value) if field and field.enum_type else None
        return timestamp, snr_data, enum.name if enum else value
    except (starlink_grpc.GrpcError, grpc.RpcError, AttributeError, ValueError) as e:
        logger.warning("Failed getting obstruction map data: %s", e)
        return None


class ObstructionMapState:
    def __init__(self):
        self.clear()

    def clear(self):
        self.previous_snr_data = None
        self.previous_frame = None
        self.hold_coord = None

    def select_coordinate(self, snr_data, frame):
        if (self.previous_snr_data is None or frame != self.previous_frame or
                snr_data.shape != self.previous_snr_data.shape):
            self.previous_snr_data = np.zeros_like(snr_data)
            self.previous_frame = frame
            self.hold_coord = None

        coords = np.argwhere(np.bitwise_xor(self.previous_snr_data, snr_data) == 1)
        if coords.size > 0:
            self.hold_coord = coords[-1]
        self.previous_snr_data = snr_data
        return self.hold_coord


def save_obstruction_sample(directory, sample, state):
    timestamp, snr_data, frame = sample
    coord = state.select_coordinate(snr_data, frame)
    if coord is None:
        return

    day_directory = Path(directory) / timestamp.strftime("%Y-%m-%d")
    ensure_directory(str(day_directory))
    filename = "obstruction-data-{}-{}.csv".format(
        DISH_ID, timestamp.strftime("%Y-%m-%d-%H-00-00"))
    with open(day_directory / filename, 'a', newline='') as csvfile:
        csv.writer(csvfile).writerow([
            timestamp.strftime("%Y-%m-%d %H:%M:%S"), coord[0], coord[1], frame])


def ri_index(timestamp):
    return int(timestamp.timestamp() - 12) // 15


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
    state = ObstructionMapState()
    last_attempted_ri = None
    deadline = time.monotonic()

    def reset_at_boundary(now):
        nonlocal last_attempted_ri
        ri = ri_index(now)
        if last_attempted_ri == ri:
            return
        if last_attempted_ri is None and now.second not in {12, 27, 42, 57}:
            last_attempted_ri = ri
            return

        last_attempted_ri = ri
        try:
            starlink_grpc.reset_obstruction_map(context)
        except (starlink_grpc.GrpcError, grpc.RpcError):
            logger.exception("Failed resetting obstruction map at RI boundary")
            return
        state.clear()

    try:
        while not stop_event.is_set():
            reset_at_boundary(datetime.now(timezone.utc))
            if not stop_event.is_set():
                sample = get_obstruction_snapshot(context)
                if sample is not None:
                    save_obstruction_sample(TLE_DATA_DIR, sample, state)
                if not stop_event.is_set():
                    reset_at_boundary(datetime.now(timezone.utc))

            deadline += 1
            now = time.monotonic()
            while deadline <= now:
                deadline += 1
            if stop_event.wait(deadline - now):
                break
    finally:
        context.close()
