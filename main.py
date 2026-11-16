import argparse
import logging
import os
import signal
import threading
import time

import schedule

logger = logging.getLogger(__name__)


def positive_int(value):
    interval = int(value)
    if interval <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return interval


def main():
    parser = argparse.ArgumentParser(description="Collect Starlink status, obstruction maps, and TLEs.")
    parser.add_argument("--results", default=os.getenv("DATA_DIR", "data"),
                        help="Results directory (default: DATA_DIR or ./data)")
    parser.add_argument("--status-interval", type=positive_int, default=60,
                        help="Seconds between gRPC status snapshots (default: 60)")
    args = parser.parse_args()
    # Collectors read configuration at import time, so set the directory first.
    os.environ["DATA_DIR"] = args.results

    from config import ENABLE_IRTT
    from latency import irtt_ping
    from satellite import load_satellites, collect_obstruction_data
    from dish import grpc_get_status
    from util import raise_worker_failure, run

    scheduler = schedule.Scheduler()
    if ENABLE_IRTT:
        scheduler.every(1).hours.at(":00").do(run, irtt_ping).tag("Latency")

    # scheduler.every(1).hours.at(":00").do(run, icmp_ping).tag("Latency")
    scheduler.every(args.status_interval).seconds.do(run, grpc_get_status).tag("gRPC")
    scheduler.every(6).hours.do(run, load_satellites).tag("TLE")

    stop_event = threading.Event()
    run(grpc_get_status)
    obstruction_thread = run(collect_obstruction_data, stop_event)
    run(load_satellites)
    for job in scheduler.jobs:
        logger.info("%s: %s", job.tags, job.next_run)

    previous_sigterm_handler = signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    try:
        while not stop_event.is_set():
            raise_worker_failure()
            scheduler.run_pending()
            time.sleep(0.5)
    finally:
        stop_event.set()
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
        if obstruction_thread is not None:
            obstruction_thread.join(timeout=20)


if __name__ == "__main__":
    main()
