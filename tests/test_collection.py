import csv
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
import util


class FakeGrpcError(Exception):
    pass


class FakeClock:
    def __init__(self, wall_time):
        self.wall_time = wall_time
        self.monotonic_time = 0

    def advance(self, seconds):
        self.wall_time += timedelta(seconds=seconds)
        self.monotonic_time += seconds

    def now(self, _timezone=None):
        return self.wall_time

    def monotonic(self):
        return self.monotonic_time


class FakeStopEvent:
    def __init__(self, clock, stop_after_waits):
        self.clock = clock
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, timeout):
        self.waits.append(timeout)
        self.clock.advance(timeout)
        self.stopped = len(self.waits) >= self.stop_after_waits
        return self.stopped


_satellite = None


def satellite_module():
    global _satellite
    if _satellite is None:
        with patch.dict(sys.modules, {"starlink_grpc": Mock(GrpcError=FakeGrpcError)}):
            _satellite = importlib.import_module("satellite")
    return _satellite


class StartupTests(unittest.TestCase):
    def test_startup_jobs_and_results_directory(self):
        collectors = {name: Mock(__name__=name) for name in (
            "grpc_get_status", "collect_obstruction_data", "load_satellites")}
        modules = {
            "dish": Mock(grpc_get_status=collectors["grpc_get_status"]),
            "satellite": Mock(**{k: v for k, v in collectors.items() if k != "grpc_get_status"}),
            "latency": Mock(),
        }
        scheduler = main.schedule.Scheduler()
        with patch.dict(sys.modules, modules), patch.dict(os.environ), \
                 patch.object(sys, "argv", ["main.py", "--results", "/tmp/results",
                                             "--status-interval", "17"]), \
                patch("config.ENABLE_IRTT", False), patch("util.run") as run, \
                patch.object(main.schedule, "Scheduler", return_value=scheduler), \
                patch.object(main.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main.main()
            self.assertEqual(os.environ["DATA_DIR"], "/tmp/results")
            self.assertEqual([call.args[0] for call in run.call_args_list], list(collectors.values()))
            self.assertTrue(run.call_args_list[1].args[1].is_set())
        self.assertEqual(len(scheduler.jobs), 2)
        status_job = next(job for job in scheduler.jobs if "gRPC" in job.tags)
        self.assertEqual((status_job.interval, status_job.unit), (17, "seconds"))
        self.assertEqual(next(job for job in scheduler.jobs if "TLE" in job.tags).interval, 3)

    def test_status_interval_must_be_positive(self):
        with patch.object(sys, "argv", ["main.py", "--status-interval", "0"]), \
                self.assertRaises(SystemExit):
            main.main()

    def test_overlapping_job_is_skipped(self):
        started = threading.Event()
        release = threading.Event()

        def collect():
            started.set()
            release.wait(5)

        try:
            util.run(collect)
            self.assertTrue(started.wait(2))
            thread = util._running[collect]
            util.run(collect)
            self.assertIs(util._running[collect], thread)
        finally:
            release.set()
            util._running[collect].join(2)
            del util._running[collect]

    def test_worker_failure_reaches_main_thread(self):
        def fail():
            raise ValueError("broken worker")

        with self.assertLogs(level="ERROR"):
            util.run(fail)
            thread = util._running[fail]
            self.assertTrue(thread.daemon)
            thread.join(2)
        try:
            with self.assertRaisesRegex(RuntimeError, "Background job failed") as raised:
                util.raise_worker_failure()
            self.assertIsInstance(raised.exception.__cause__, ValueError)
        finally:
            del util._running[fail]


class TLETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.satellite = satellite_module()

    def test_missing_fresh_and_stale_tles(self):
        fixture = Path(__file__).parent / "fixtures/inference/starlink-tle-2026-01-11-00-33-00.txt"
        data = b"".join(fixture.read_bytes().splitlines(keepends=True)[:3])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.satellite, "TLE_DATA_DIR", Path(directory)), \
                patch.object(self.satellite, "satellites", None), \
                patch.object(self.satellite, "urlopen") as fetch:
            fetch.return_value.__enter__.return_value.read.return_value = data
            self.satellite.load_satellites()
            fetch.assert_called_once_with(self.satellite.TLE_URL, timeout=60)
            snapshot, = Path(directory).glob("*/starlink-tle-*.txt")
            self.assertEqual(snapshot.read_bytes(), data)
            self.assertEqual(len(self.satellite.satellites), 1)

            fetch.reset_mock()
            self.satellite.load_satellites()
            fetch.assert_not_called()

            stale = time.time() - 3 * 3600 - 1
            os.utime(snapshot, (stale, stale))
            self.satellite.load_satellites()
            fetch.assert_called_once_with(self.satellite.TLE_URL, timeout=60)

    def test_download_timeout_preserves_previous_satellites_and_allows_retry(self):
        fixture = Path(__file__).parent / "fixtures/inference/starlink-tle-2026-01-11-00-33-00.txt"
        data = b"".join(fixture.read_bytes().splitlines(keepends=True)[:3])
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.satellite, "TLE_DATA_DIR", Path(directory)), \
                patch.object(self.satellite, "satellites", ["previous"]), \
                patch.object(self.satellite, "urlopen") as fetch:
            response = fetch.return_value.__enter__.return_value
            response.read.side_effect = [TimeoutError("read timed out"), b"", data]

            for message in ("read timed out", "contained no TLEs"):
                with self.assertLogs("satellite", level="ERROR") as logs:
                    self.satellite.load_satellites()
                self.assertIn(message, logs.output[0])
                self.assertEqual(self.satellite.satellites, ["previous"])
                self.assertEqual(list(Path(directory).glob("*/starlink-tle-*")), [])

            self.satellite.load_satellites()
            self.assertEqual(fetch.call_count, 3)
            snapshot, = Path(directory).glob("*/starlink-tle-*")
            self.assertEqual(snapshot.suffix, ".txt")
            self.assertEqual(snapshot.read_bytes(), data)


class ObstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.satellite = satellite_module()

    def test_csv_rotates_at_hour_and_day_boundaries(self):
        samples = [(datetime(2026, 1, 1, 23, 59, 57, tzinfo=timezone.utc),
                    self.satellite.np.array([[1, 0], [0, 0]]), "FRAME_EARTH")]
        samples.extend(
            (datetime(2026, 1, 1, 23, 59, second, tzinfo=timezone.utc),
             self.satellite.np.array([[1, 0], [0, 0]]), "FRAME_EARTH")
            for second in (58, 59))
        samples.extend((datetime(2026, 1, 2, 0, second, tzinfo=timezone.utc),
                        self.satellite.np.array([[1, 0], [0, 0]]), "FRAME_EARTH")
                       for second in range(2))
        with tempfile.TemporaryDirectory() as directory, \
                 patch.object(self.satellite, "DISH_ID", "dish"):
            state = self.satellite.ObstructionMapState()
            for sample in samples:
                self.satellite.save_obstruction_sample(directory, sample, state)

            before_midnight = Path(directory) / "2026-01-01" / \
                "obstruction-data-dish-2026-01-01-23-00-00.csv"
            after_midnight = Path(directory) / "2026-01-02" / \
                "obstruction-data-dish-2026-01-02-00-00-00.csv"
            self.assertEqual(len(before_midnight.read_text().splitlines()), 3)
            self.assertEqual(len(after_midnight.read_text().splitlines()), 2)

    def test_snapshot_uses_acquisition_timestamp_and_response_frame(self):
        field = SimpleNamespace(enum_type=SimpleNamespace(values_by_number={
            1: SimpleNamespace(name="FRAME_EARTH"),
            2: SimpleNamespace(name="FRAME_UT")}))

        def response(frame):
            return SimpleNamespace(
                num_rows=1, num_cols=2, snr=[0.5, -1], map_reference_frame=frame,
                DESCRIPTOR=SimpleNamespace(fields_by_name={"map_reference_frame": field}))

        times = [datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)
                 for second in (1, 2, 3)]
        with patch.object(self.satellite, "datetime", Mock(now=Mock(side_effect=times))), \
                patch.object(self.satellite.starlink_grpc, "get_obstruction_map",
                             side_effect=[response(1), response(2), response(99)]):
            samples = [self.satellite.get_obstruction_snapshot(object()) for _ in times]

        self.assertEqual([sample[0] for sample in samples], times)
        self.assertEqual([sample[1].tolist() for sample in samples], [[[1, 0]]] * 3)
        self.assertEqual([sample[2] for sample in samples], ["FRAME_EARTH", "FRAME_UT", 99])

    def test_continuous_polling_starts_immediately_and_resets_at_boundary(self):
        clock = FakeClock(datetime(2026, 1, 1, 0, 0, 11, tzinfo=timezone.utc))
        event = FakeStopEvent(clock, stop_after_waits=3)
        context = Mock()
        poll_times = []

        def poll(_context):
            poll_times.append(clock.wall_time)
            return (clock.wall_time, self.satellite.np.array([[1]]), "FRAME_EARTH")

        class ClockDateTime:
            @classmethod
            def now(cls, tz=None):
                return clock.now(tz)

        with patch.object(self.satellite, "datetime", ClockDateTime), \
                patch.object(self.satellite.time, "monotonic", clock.monotonic), \
                patch.object(self.satellite.starlink_grpc, "ChannelContext", return_value=context), \
                patch.object(self.satellite.starlink_grpc, "reset_obstruction_map") as reset, \
                patch.object(self.satellite, "get_obstruction_snapshot", side_effect=poll), \
                patch.object(self.satellite, "save_obstruction_sample") as save:
            self.satellite.collect_obstruction_data(event)

        self.assertEqual([sample.second for sample in poll_times], [11, 12, 13])
        self.assertEqual(reset.call_count, 1)
        self.assertEqual(save.call_count, 3)
        self.assertEqual(event.waits, [1, 1, 1])
        context.close.assert_called_once_with()

    def test_slow_poll_skips_boundary_without_catch_up_burst(self):
        clock = FakeClock(datetime(2026, 1, 1, 0, 0, 11, tzinfo=timezone.utc))
        event = FakeStopEvent(clock, stop_after_waits=2)
        context = Mock()
        poll_times = []

        def slow_poll(_context):
            poll_times.append(clock.wall_time)
            clock.advance(2.4)
            return None

        class ClockDateTime:
            @classmethod
            def now(cls, tz=None):
                return clock.now(tz)

        with patch.object(self.satellite, "datetime", ClockDateTime), \
                patch.object(self.satellite.time, "monotonic", clock.monotonic), \
                patch.object(self.satellite.starlink_grpc, "ChannelContext", return_value=context), \
                patch.object(self.satellite.starlink_grpc, "reset_obstruction_map") as reset, \
                patch.object(self.satellite, "get_obstruction_snapshot", side_effect=slow_poll):
            self.satellite.collect_obstruction_data(event)

        self.assertEqual(len(poll_times), 2)
        self.assertEqual((poll_times[1] - poll_times[0]).total_seconds(), 3)
        self.assertEqual([round(wait, 1) for wait in event.waits], [0.6, 0.6])
        reset.assert_called_once_with(context)
        context.close.assert_called_once_with()

    def test_successful_reset_clears_delta_baseline(self):
        clock = FakeClock(datetime(2026, 1, 1, 0, 0, 11, tzinfo=timezone.utc))
        event = FakeStopEvent(clock, stop_after_waits=2)
        context = Mock()
        values = iter(([[1]], [[0]]))

        def poll(_context):
            return (clock.wall_time, self.satellite.np.array(next(values)), "FRAME_EARTH")

        class ClockDateTime:
            @classmethod
            def now(cls, tz=None):
                return clock.now(tz)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.satellite, "datetime", ClockDateTime), \
                patch.object(self.satellite.time, "monotonic", clock.monotonic), \
                patch.object(self.satellite, "TLE_DATA_DIR", Path(directory)), \
                patch.object(self.satellite, "DISH_ID", "dish"), \
                patch.object(self.satellite.starlink_grpc, "ChannelContext", return_value=context), \
                patch.object(self.satellite.starlink_grpc, "reset_obstruction_map") as reset, \
                patch.object(self.satellite, "get_obstruction_snapshot", side_effect=poll):
            self.satellite.collect_obstruction_data(event)
            path = next(Path(directory).rglob("*.csv"))
            with path.open() as stream:
                rows = list(csv.reader(stream))

        self.assertEqual(reset.call_count, 1)
        self.assertEqual([row[0] for row in rows], [
            "2026-01-01 00:00:11", "2026-01-01 00:00:12"])
        self.assertTrue(all(row[1:3] == ["", ""] for row in rows))
        context.close.assert_called_once_with()

    def test_poll_and_reset_failures_recover_without_retrying_same_ri(self):
        clock = FakeClock(datetime(2026, 1, 1, 0, 0, 11, tzinfo=timezone.utc))
        event = FakeStopEvent(clock, stop_after_waits=3)
        context = Mock()
        resets = []
        polls = []

        def reset(_context):
            resets.append(clock.wall_time)
            if len(resets) == 1:
                raise FakeGrpcError("reset failed")

        def poll(_context):
            polls.append(clock.wall_time)
            if len(polls) == 1:
                clock.advance(1)
                return None
            if len(polls) == 2:
                clock.advance(14)
            return (clock.wall_time, self.satellite.np.array([[1]]), "FRAME_EARTH")

        class ClockDateTime:
            @classmethod
            def now(cls, tz=None):
                return clock.now(tz)

        with patch.object(self.satellite, "datetime", ClockDateTime), \
                patch.object(self.satellite.time, "monotonic", clock.monotonic), \
                patch.object(self.satellite.starlink_grpc, "ChannelContext", return_value=context), \
                patch.object(self.satellite.starlink_grpc, "reset_obstruction_map", side_effect=reset), \
                patch.object(self.satellite, "get_obstruction_snapshot", side_effect=poll), \
                patch.object(self.satellite, "save_obstruction_sample") as save, \
                self.assertLogs("satellite", level="ERROR"):
            self.satellite.collect_obstruction_data(event)

        self.assertEqual([timestamp.second for timestamp in resets], [12, 27])
        self.assertEqual(len(polls), 3)
        self.assertEqual(save.call_count, 2)
        context.close.assert_called_once_with()

    def test_sample_timestamp_frame_change_and_rotation(self):
        state = self.satellite.ObstructionMapState()
        samples = [
            (datetime(2026, 1, 1, 23, 59, 59, tzinfo=timezone.utc),
             self.satellite.np.array([[1, 0]]), "FRAME_EARTH"),
            (datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
             self.satellite.np.array([[1, 0]]), "FRAME_EARTH"),
            (datetime(2026, 1, 2, 0, 0, 2, tzinfo=timezone.utc),
             self.satellite.np.array([[0, 1]]), "FRAME_UT"),
        ]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.satellite, "DISH_ID", "dish"):
            for sample in samples:
                self.satellite.save_obstruction_sample(directory, sample, state)
            paths = sorted(Path(directory).rglob("*.csv"))
            rows = []
            for path in paths:
                with path.open() as stream:
                    rows.extend(csv.reader(stream))

        self.assertEqual([row[0] for row in rows], [
            "2026-01-01 23:59:59", "2026-01-02 00:00:00", "2026-01-02 00:00:02"])
        self.assertEqual([row[3] for row in rows], ["FRAME_EARTH", "FRAME_EARTH", "FRAME_UT"])
        self.assertEqual(state.select_coordinates(
            self.satellite.np.zeros((2, 1), dtype=int), "FRAME_UT").size, 0)
        state.clear()
        self.assertEqual(state.select_coordinates(
            self.satellite.np.zeros((2, 1), dtype=int), "FRAME_UT").size, 0)
