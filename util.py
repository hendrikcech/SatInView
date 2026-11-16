import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

from config import DATA_DIR

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s')


def date_time_string() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")


def ensure_directory(name: str):
    return Path(name).mkdir(parents=True, exist_ok=True)


def ensure_data_directory(directory: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ensure_directory(str(Path(directory).joinpath(today)))
    return today


def test_command(command: str) -> bool:
    return which(command) is not None


def failed(e: str) -> None:
    with open("{}/failed.txt".format(DATA_DIR), "a+") as f:
        f.write("{}: {}\n".format(date_time_string(), e))


_running = {}
_failures = queue.Queue()


def run(func, *args):
    previous = _running.get(func)
    if previous is not None and previous.is_alive():
        logging.warning("Skipping overlapping job: %s", func.__name__)
        return previous

    def collect():
        try:
            func(*args)
        except BaseException as error:
            logging.exception("Job failed: %s", func.__name__)
            _failures.put(error)

    job_thread = threading.Thread(target=collect, name=func.__name__, daemon=True)
    _running[func] = job_thread
    job_thread.start()
    return job_thread


def raise_worker_failure():
    try:
        error = _failures.get_nowait()
    except queue.Empty:
        return
    raise RuntimeError("Background job failed") from error
