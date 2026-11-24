import csv
import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import grpc

import process_obstruction_data


class ReferenceFrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global satellite
        satellite = importlib.import_module("satellite")
        cls.grpc_patch = patch.object(satellite, "starlink_grpc", importlib.import_module("starlink_grpc"))
        cls.grpc_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.grpc_patch.stop()

    def test_capture_and_save_frames_from_each_response(self):
        field = SimpleNamespace(enum_type=SimpleNamespace(values_by_number={
            1: SimpleNamespace(name="FRAME_EARTH"), 2: SimpleNamespace(name="FRAME_UT")}))
        def response(frame):
            return SimpleNamespace(num_rows=1, num_cols=2, snr=[0.5, -1],
                                   map_reference_frame=frame,
                                   DESCRIPTOR=SimpleNamespace(fields_by_name={"map_reference_frame": field}))

        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.object(satellite, "datetime", Mock(now=Mock(return_value=timestamp))), \
                patch.object(satellite.starlink_grpc, "get_obstruction_map",
                             side_effect=[response(1), response(2), response(99), grpc.RpcError()]), \
                self.assertLogs("satellite", level="WARNING"):
            samples = [satellite.get_obstruction_snapshot(object()) for _ in range(4)]
        with tempfile.TemporaryDirectory() as directory:
            state = satellite.ObstructionMapState()
            for sample in samples:
                if sample is not None:
                    satellite.save_obstruction_sample(directory, sample, state)
            path = next(Path(directory).rglob("*.csv"))
            with path.open() as stream:
                rows = list(csv.reader(stream))
        self.assertEqual([row[3] for row in rows], ["FRAME_EARTH", "FRAME_UT", "99"])

    def test_missing_frame_is_empty(self):
        response = SimpleNamespace(num_rows=1, num_cols=1, snr=[1],
                                   DESCRIPTOR=SimpleNamespace(fields_by_name={}))
        with patch.object(satellite.starlink_grpc, "get_obstruction_map", return_value=response):
            sample = satellite.get_obstruction_snapshot(object())
        self.assertIsNone(sample[2])

    def test_processors_accept_mixed_old_and_new_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.csv"
            path.write_text("2026-01-01 00:00:00,62,62\n2026-01-01 00:00:01,62,63,FRAME_EARTH\n")
            result = process_obstruction_data.process_obstruction_data(path)
            self.assertEqual(result["X"].tolist(), [62, 63])


if __name__ == "__main__":
    unittest.main()
