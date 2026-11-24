import subprocess
import sys
from pathlib import Path

import pandas as pd


FIXTURES = Path(__file__).parent / "fixtures" / "inference"


def test_historical_inference_snapshot(tmp_path):
    output = tmp_path / "inferred.csv"
    subprocess.run([
        sys.executable, "process_obstruction_data.py",
        "--obs", str(FIXTURES / "obstruction.csv"),
        "--tles", str(FIXTURES / "starlink-tle-2026-01-11-00-33-00.txt"),
        "--lat", "48.267548118257835", "--lon", "11.668237289960858",
        "--ele", "492", "--cpus", "1", "-o", str(output),
    ], check=True, capture_output=True, text=True, timeout=300)

    expected = pd.read_csv(FIXTURES / "expected.csv")
    actual = pd.read_csv(output)
    # The historical script filled missing seconds with blank geometry; the current
    # script emits only real observations rather than inventing those rows.
    assert expected.Distance.isna().sum() == 20
    expected = expected[expected.Distance.notna()].reset_index(drop=True)
    calculated = ["Distance", "MatchedElevation", "MatchedAzimuth"]
    pd.testing.assert_frame_equal(actual.drop(columns=calculated),
                                  expected.drop(columns=calculated), check_exact=True)
    pd.testing.assert_frame_equal(actual[calculated], expected[calculated],
                                  check_exact=False, rtol=0, atol=0.01)
