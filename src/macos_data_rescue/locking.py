"""Serialize scan/copy writers without stale PID files after interruption."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os

from .errors import RescueError
from .manifest import load_config


@contextmanager
def job_lock(job_dir):
    load_config(job_dir)  # Validate source guards before creating a lock file.
    fd = os.open(job_dir / ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RescueError("another scan/copy is running for this job") from exc
        yield
    finally:
        os.close(fd)


def exclusive_job(function):
    @wraps(function)
    def wrapped(job_dir, *args, **kwargs):
        with job_lock(job_dir):
            return function(job_dir, *args, **kwargs)
    return wrapped
