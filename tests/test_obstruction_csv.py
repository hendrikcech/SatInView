import ast
import csv
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import pytest

import process_obstruction_data as processing

with patch.dict(sys.modules, {"starlink_grpc": Mock(GrpcError=Exception)}):
    satellite = importlib.import_module("satellite")


UTC = timezone.utc


def _timestamp(second, microsecond=0, day=1, hour=0, minute=0):
    return datetime(2026, 1, day, hour, minute, second, microsecond, tzinfo=UTC)


def _csv_rows(directory):
    return [row for path in Path(directory).rglob("*.csv")
            for row in csv.reader(path.open(newline=""))]


def test_capture_writes_new_pixels_only_and_one_blank_row_for_each_quiet_poll(tmp_path):
    state = satellite.ObstructionMapState()
    maps = [
        (_timestamp(0), [[1, 0], [0, 0]]),  # startup map is a baseline
        (_timestamp(1), [[1, 1], [1, 1]]),  # three additions
        (_timestamp(2), [[1, 1], [1, 1]]),  # unchanged
        (_timestamp(3), [[0, 1], [1, 1]]),  # disappearance
        (_timestamp(4), [[1, 1], [1, 1]]),  # revisit
    ]

    for timestamp, snr_data in maps:
        satellite.save_obstruction_sample(
            tmp_path, (timestamp, satellite.np.array(snr_data), "FRAME_EARTH"), state)

    rows = _csv_rows(tmp_path)
    assert [(row[1], row[2]) for row in rows] == [
        ("", ""), ("0", "1"), ("1", "0"), ("1", "1"),
        ("", ""), ("", ""), ("0", "0")]
    assert [row[0] for row in rows] == [
        "2026-01-01 00:00:00", "2026-01-01 00:00:01",
        "2026-01-01 00:00:01", "2026-01-01 00:00:01",
        "2026-01-01 00:00:02", "2026-01-01 00:00:03",
        "2026-01-01 00:00:04"]
    assert all(len(row) == 4 and row[3] == "FRAME_EARTH" for row in rows)


def test_poll_timestamps_keep_fractional_identity_and_shared_pixel_time(tmp_path):
    state = satellite.ObstructionMapState()
    for timestamp, pixels in [
        (_timestamp(0), [[0, 0]]),
        (_timestamp(0, 123456), [[1, 1]]),
        (_timestamp(0, 654321), [[1, 1]]),
    ]:
        satellite.save_obstruction_sample(
            tmp_path, (timestamp, satellite.np.array(pixels), "FRAME_UT"), state)

    rows = _csv_rows(tmp_path)
    assert [row[0] for row in rows] == [
        "2026-01-01 00:00:00", "2026-01-01 00:00:00.123456",
        "2026-01-01 00:00:00.123456", "2026-01-01 00:00:00.654321"]
    assert rows[1][1:3] == ["0", "0"]
    assert rows[2][1:3] == ["0", "1"]
    assert rows[1][0] == rows[2][0] != rows[3][0]


def test_frame_shape_and_successful_reset_start_fresh_comparisons():
    state = satellite.ObstructionMapState()
    assert state.select_coordinates(satellite.np.array([[1, 0]]), "FRAME_EARTH").tolist() == []
    assert state.select_coordinates(satellite.np.array([[1, 0]]), "FRAME_EARTH").tolist() == []
    assert state.select_coordinates(satellite.np.array([[0, 1]]), "FRAME_UT").tolist() == []
    assert state.select_coordinates(satellite.np.array([[1, 1]]), "FRAME_UT").tolist() == [[0, 0]]
    assert state.select_coordinates(satellite.np.array([[1], [1]]), "FRAME_UT").tolist() == []
    assert state.select_coordinates(satellite.np.array([[0], [1]]), "FRAME_UT").tolist() == []
    assert state.select_coordinates(satellite.np.array([[1], [1]]), "FRAME_UT").tolist() == [[0, 0]]

    state.clear()
    assert state.select_coordinates(satellite.np.array([[1], [1]]), "FRAME_UT").tolist() == [[0, 0], [1, 0]]

    reset_before_first_poll = satellite.ObstructionMapState()
    reset_before_first_poll.clear()
    assert reset_before_first_poll.select_coordinates(
        satellite.np.array([[0, 1]]), "FRAME_UT").tolist() == [[0, 1]]


def test_capture_rotates_at_hour_and_day_boundaries(tmp_path):
    state = satellite.ObstructionMapState()
    times = [_timestamp(59, hour=23, minute=59),
             _timestamp(0, day=2), _timestamp(1, day=2)]
    for timestamp in times:
        satellite.save_obstruction_sample(
            tmp_path, (timestamp, satellite.np.zeros((1, 1), dtype=int), "FRAME_EARTH"), state)

    paths = sorted(path.relative_to(tmp_path).as_posix()
                   for path in tmp_path.rglob("*.csv"))
    assert paths == [
        "2026-01-01/obstruction-data-test-2026-01-01-23-00-00.csv",
        "2026-01-02/obstruction-data-test-2026-01-02-00-00-00.csv"]
    counts = {path.relative_to(tmp_path).parts[0]: len(list(csv.reader(path.open())))
              for path in tmp_path.rglob("*.csv")}
    assert counts == {"2026-01-01": 1, "2026-01-02": 2}


class _Clock:
    def __init__(self):
        self.wall = _timestamp(11)
        self.monotonic_time = 0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.monotonic_time += seconds


class _StopAfterWaits:
    def __init__(self, clock, count):
        self.clock = clock
        self.count = count
        self.waits = 0

    def is_set(self):
        return self.waits >= self.count

    def wait(self, timeout):
        self.clock.advance(timeout)
        self.waits += 1
        return self.is_set()


def test_failed_poll_writes_nothing_but_successful_empty_poll_writes_blank(tmp_path):
    clock = _Clock()
    context = Mock()

    class ClockDateTime:
        @classmethod
        def now(cls, tz=None):
            return clock.wall

    event = _StopAfterWaits(clock, 2)
    with patch.object(satellite, "datetime", ClockDateTime), \
            patch.object(satellite.time, "monotonic", lambda: clock.monotonic_time), \
            patch.object(satellite, "TLE_DATA_DIR", tmp_path), \
            patch.object(satellite, "DISH_ID", "dish"), \
            patch.object(satellite.starlink_grpc, "ChannelContext", return_value=context), \
            patch.object(satellite.starlink_grpc, "reset_obstruction_map"), \
            patch.object(satellite, "get_obstruction_snapshot", side_effect=[
                None, (_timestamp(12), satellite.np.zeros((1, 1), dtype=int), "FRAME_EARTH")]):
        satellite.collect_obstruction_data(event)

    rows = _csv_rows(tmp_path)
    assert rows == [["2026-01-01 00:00:12", "", "", "FRAME_EARTH"]]
    context.close.assert_called_once_with()


def test_csv_reader_accepts_three_and_four_columns_and_sorts_real_rows(tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text(
        "2026-01-01 00:00:02,62,62\n"
        "2026-01-01 00:00:01.123456,62,63,FRAME_UT\n"
        "2026-01-01 00:00:01.123456,61,64\n"
        "2026-01-01 00:00:03,,,FRAME_UT\n")

    result = processing.process_obstruction_data(path)

    assert result.Timestamp.tolist() == list(pd.to_datetime([
        "2026-01-01 00:00:01.123456", "2026-01-01 00:00:01.123456",
        "2026-01-01 00:00:02"], format="mixed", utc=True))
    assert {tuple(row) for row in result[["Y", "X"]].values.tolist()} == {
        (62, 63), (61, 64), (62, 62)}
    assert result[["Elevation", "Azimuth"]].notna().all().all()


@pytest.mark.parametrize("contents", ["", "2026-01-01 00:00:00,,,FRAME_EARTH\n"])
def test_empty_or_blank_only_csv_has_no_geometry_rows(tmp_path, contents):
    path = tmp_path / "empty.csv"
    path.write_text(contents)

    result = processing.process_obstruction_data(path)

    assert result.empty
    assert list(result.columns) == ["Timestamp", "Y", "X", "Elevation", "Azimuth"]
    assert processing.get_compute_intervals(processing.compute_separation(result)) == []


def test_cli_writes_headers_for_an_empty_capture(tmp_path):
    observation = tmp_path / "empty.csv"
    observation.write_text("2026-01-01 00:00:00,,,FRAME_EARTH\n")
    output = tmp_path / "result.csv"

    class EmptyPool:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def imap_unordered(self, _function, _args):
            return iter(())

    with patch.object(sys, "argv", [
            "process_obstruction_data.py", "--obs", str(observation), "--tles", "unused.txt",
            "-o", str(output), "--cpus", "1"]), \
            patch.object(processing.TLEManager, "init"), \
            patch.object(processing.wgs84, "latlon", return_value=None), \
            patch.object(processing.multiprocessing, "Pool", return_value=EmptyPool()):
        processing.main_cli()

    assert output.read_text().strip() == (
        "Timestamp,Connected_Satellite,Generation,Distance,ObservedElevation,"
        "MatchedElevation,ObservedAzimuth,MatchedAzimuth")


def test_reader_rejects_partial_coordinates(tmp_path):
    path = tmp_path / "partial.csv"
    path.write_text("2026-01-01 00:00:00,62,,FRAME_EARTH\n")

    with pytest.raises(ValueError, match="only one coordinate"):
        processing.process_obstruction_data(path)


def test_matcher_rejects_multiple_pixels_at_one_timestamp():
    timestamp = _timestamp(0)
    positions = [(timestamp, (20, 30)), (timestamp, (21, 31))]

    with pytest.raises(ValueError, match="Multiple obstruction pixels share a timestamp"):
        processing.find_matching_satellites([], None, positions)


def test_matcher_orders_unique_fractional_samples_by_timestamp():
    early = _timestamp(0, 123456)
    late = _timestamp(0, 654321)
    seen = []

    class FakeTimescale:
        def utc(self, *parts):
            return parts

    class FakeSatellite:
        def __sub__(self, _observer):
            return self

        def at(self, timestamp):
            seen.append(timestamp)
            return SimpleNamespace(altaz=lambda: (
                SimpleNamespace(degrees=60), SimpleNamespace(degrees=30), None))

    with patch.object(processing.load, "timescale", return_value=FakeTimescale()):
        processing.find_matching_satellites(
            [FakeSatellite()], object(), [(late, (20, 40)), (early, (30, 50))])

    assert seen[0][:5] == (2026, 1, 1, 0, 0)
    assert seen[0][5] == pytest.approx(0.123456)
    assert seen[1][5] == pytest.approx(0.654321)


def test_compute_intervals_split_at_actual_fractional_sample_times():
    start = pd.Timestamp(_timestamp(0, 120000))
    split = pd.Timestamp(_timestamp(1, 350000))
    end = pd.Timestamp(_timestamp(2, 700000))
    df = pd.DataFrame({
        "Timestamp": [start, split, end],
        "Separation": [float("nan"), 4, 0],
    })

    intervals = processing.get_compute_intervals(df)

    assert intervals == [(start, 1.23), (split, 2.35)]


def test_compute_intervals_split_on_ri_changes_without_boundary_samples():
    times = [pd.Timestamp(_timestamp(second, 200000)) for second in (11, 13, 28)]
    df = pd.DataFrame({"Timestamp": times, "Separation": [float("nan"), 0, 0]})

    assert processing.get_compute_intervals(df) == [
        (times[0], 2.0), (times[1], 15.0), (times[2], 1.0)]


def test_compute_intervals_do_not_split_multiple_samples_inside_ri_second():
    times = [pd.Timestamp(_timestamp(12, 100000)), pd.Timestamp(_timestamp(12, 800000))]
    df = pd.DataFrame({"Timestamp": times, "Separation": [float("nan"), 0]})

    assert processing.get_compute_intervals(df) == [(times[0], 1.7)]


@pytest.mark.parametrize("notebook", ["findmatch-FrameUT.ipynb", "findmatch-FrameEarth.ipynb"])
@pytest.mark.parametrize("duplicate_source", ["capture", "merged"])
def test_frame_notebooks_reject_duplicates_before_three_point_sampling(
        tmp_path, notebook, duplicate_source):
    document = json.loads(Path(notebook).read_text())
    source = next("".join(cell["source"]) for cell in document["cells"]
                  if cell["cell_type"] == "code" and
                  "def process_observed_data(filename, start_time, merged_data_file):" in "".join(cell["source"]))
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and
                    node.name == "process_observed_data" and len(node.args.args) == 3)
    namespace = {"pd": pd}
    exec(compile(ast.Module(body=[function], type_ignores=[]), f"<{notebook} source>", "exec"), namespace)

    capture = tmp_path / "capture.csv"
    merged = tmp_path / "merged.csv"
    if duplicate_source == "capture":
        times = ["00", "01", "01", "02", "03", "04"]
    else:
        times = ["00", "01", "02", "03", "04"]
        merged_times = ["00", "01", "01", "02", "03", "04"]
        merged.write_text("Timestamp,Elevation,Azimuth\n" + "".join(
            f"2026-01-01 00:00:{second},70,120\n" for second in merged_times))
    capture.write_text("".join(
        f"2026-01-01 00:00:{second},62,62\n" for second in times))

    with pytest.raises(ValueError, match="Multiple .* pixels share a timestamp"):
        namespace["process_observed_data"](
            capture, "2026-01-01 00:00:00+00:00", merged)
