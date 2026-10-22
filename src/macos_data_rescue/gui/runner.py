"""Run CLI commands for the GUI in a child process, safely and observably.

Threading contract: the runner never touches Tk. Worker threads only put
events on a ``queue.Queue`` that the Tk main loop drains. ``busy`` stays True
until the child process (and its keep-awake helper) has been reaped, so the
GUI can never report idle, or start a second writer, while a child lives.

Each command runs in its own session (process group) so Stop can signal the
CLI together with its spawned copy/scan workers: SIGINT first (the CLI
records an interrupted activity and returns rows to a resumable state), then
SIGTERM, then SIGKILL.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .commands import CommandSpec

LOG = logging.getLogger(__name__)

CAFFEINATE = "/usr/bin/caffeinate"
STOP_GRACE_SECONDS = 15.0
TERM_GRACE_SECONDS = 5.0
KILL_REPORT_INTERVAL = 10.0


@dataclass(frozen=True)
class OutputLine:
    step: int
    spec: CommandSpec
    text: str


@dataclass(frozen=True)
class RunnerNotice:
    """Runner lifecycle message; ``key`` is an i18n message key."""

    level: str
    key: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunFinished:
    steps: tuple[CommandSpec, ...]
    last_step: int  # index of the last step that was started (-1: none)
    returncode: int | None
    stop_requested: bool
    error: str | None = None

    @property
    def last_spec(self) -> CommandSpec | None:
        return self.steps[self.last_step] if self.last_step >= 0 else None

    @property
    def completed_all(self) -> bool:
        return (self.error is None and self.returncode == 0 and not self.stop_requested
                and self.last_step == len(self.steps) - 1)


class RunnerBusy(RuntimeError):
    pass


class KeepAwake:
    """``caffeinate -dims -w PID``: sleep is inhibited only while PID lives."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, binary: str = CAFFEINATE,
                 exists: Callable[[str], bool] = os.path.exists) -> None:
        self._popen = popen
        self._binary = binary
        self._exists = exists
        self._proc: Any = None

    def start(self, pid: int) -> None:
        if not self._exists(self._binary):
            return
        try:
            self._proc = self._popen(  # nosec B603
                [self._binary, "-dims", "-w", str(pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            LOG.warning("caffeinate could not be started: %s", exc)
            self._proc = None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # -w makes caffeinate exit with the watched child; this reaps it and
        # covers the case where the child died before caffeinate attached
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("caffeinate did not exit cleanly: %s", exc)
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                LOG.warning("caffeinate pid %s could not be reaped", getattr(proc, "pid", "?"))


def default_command(spec: CommandSpec) -> list[str]:
    return [sys.executable, "-m", "macos_data_rescue", *spec.args]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class CommandRunner:
    def __init__(
        self,
        events: queue.Queue,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        killpg: Callable[[int, int], None] = os.killpg,
        keep_awake_factory: Callable[[], KeepAwake] = KeepAwake,
        command: Callable[[CommandSpec], list[str]] = default_command,
        stop_grace: float = STOP_GRACE_SECONDS,
        term_grace: float = TERM_GRACE_SECONDS,
    ) -> None:
        self.events = events
        self._popen = popen
        self._killpg = killpg
        self._keep_awake_factory = keep_awake_factory
        self._command = command
        self._stop_grace = stop_grace
        self._term_grace = term_grace
        self._lock = threading.Lock()
        self._busy = False
        self._proc: Any = None
        self._stop = threading.Event()
        self._exited = threading.Event()
        self._stopper: threading.Thread | None = None
        self._worker: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, steps: Sequence[CommandSpec], *, keep_awake: bool = False) -> None:
        if not steps:
            raise ValueError("no command to run")
        with self._lock:
            if self._busy:
                raise RunnerBusy("an operation is already running")
            self._busy = True
            self._stop.clear()
            self._stopper = None
        self._worker = threading.Thread(target=self._run, args=(tuple(steps), keep_awake),
                                        name="rescue-runner", daemon=True)
        self._worker.start()

    def request_stop(self) -> bool:
        """Ask the running command to stop; returns at once (never blocks Tk)."""
        with self._lock:
            if not self._busy or self._stop.is_set():
                return False
            self._stop.set()
            self._stopper = threading.Thread(target=self._escalate, name="rescue-stopper", daemon=True)
            self._stopper.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        """Test helper: wait for the worker thread."""
        if self._worker is not None:
            self._worker.join(timeout)

    # -- worker thread ----------------------------------------------------

    def _run(self, steps: tuple[CommandSpec, ...], keep_awake: bool) -> None:
        last_step = -1
        returncode: int | None = None
        error: str | None = None
        try:
            for index, spec in enumerate(steps):
                if self._stop.is_set():
                    break
                last_step = index
                self.events.put(RunnerNotice("info", "log_running", {"command": spec.display()}))
                returncode, error = self._run_step(index, spec, keep_awake)
                if error is not None or returncode != 0:
                    break
        except BaseException as exc:  # never leave busy=True with nothing running
            LOG.exception("runner failed")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            stop_requested = self._stop.is_set()
            with self._lock:
                self._busy = False
                self._proc = None
            self.events.put(RunFinished(steps, last_step, returncode, stop_requested, error))

    def _run_step(self, index: int, spec: CommandSpec, keep_awake: bool) -> tuple[int | None, str | None]:
        capture: tuple[int, str] | None = None
        if spec.stdout_path is not None:
            fd, temp_name = tempfile.mkstemp(dir=spec.stdout_path.parent,
                                              prefix=f".{spec.stdout_path.name}.", suffix=".tmp")
            capture = (fd, temp_name)
        try:
            proc = self._popen(  # nosec B603
                self._command(spec),
                stdin=subprocess.DEVNULL,
                stdout=capture[0] if capture else subprocess.PIPE,
                stderr=subprocess.PIPE if capture else subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=True, env=_child_env(),
            )
        except OSError as exc:
            if capture:
                os.close(capture[0])
                _unlink(capture[1])
            return None, f"{type(exc).__name__}: {exc}"
        if capture:
            os.close(capture[0])  # the child holds its own copy
        with self._lock:
            self._exited.clear()
            self._proc = proc
        awake: KeepAwake | None = None
        error: str | None = None
        stream = proc.stderr if capture else proc.stdout
        try:
            if keep_awake:
                awake = self._keep_awake_factory()
                awake.start(proc.pid)
            for line in stream:
                self.events.put(OutputLine(index, spec, line))
        except BaseException as exc:
            LOG.exception("reading command output failed")
            error = f"{type(exc).__name__}: {exc}"
            self._terminate(proc)
        finally:
            returncode = self._reap(proc)
            with self._lock:
                self._proc = None
                self._exited.set()
            if awake is not None:
                awake.stop()
            try:
                if stream is not None:
                    stream.close()
            except OSError as exc:
                LOG.warning("closing command output failed: %s", exc)
        if capture:
            if error is None and returncode == 0 and not self._stop.is_set():
                os.replace(capture[1], spec.stdout_path)
                self.events.put(RunnerNotice("info", "log_wrote_file", {"path": str(spec.stdout_path)}))
            else:
                _unlink(capture[1])
        return returncode, error

    def _reap(self, proc: Any) -> int | None:
        # Wait without a deadline: a child stuck in uninterruptible disk I/O
        # stays "busy" rather than being reported finished while alive.
        while True:
            try:
                return proc.wait(timeout=KILL_REPORT_INTERVAL)
            except subprocess.TimeoutExpired:
                if self._stop.is_set():
                    self.events.put(RunnerNotice("warn", "log_child_still_alive", {"pid": proc.pid}))

    def _terminate(self, proc: Any) -> None:
        for sig, grace in ((signal.SIGTERM, self._term_grace), (signal.SIGKILL, None)):
            self._signal(proc, sig)
            if grace is None:
                return
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    def _signal(self, proc: Any, sig: int) -> None:
        try:
            self._killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            LOG.warning("signal %s to process group %s failed: %s", sig, proc.pid, exc)

    # -- stopper thread ---------------------------------------------------

    def _escalate(self) -> None:
        proc = self._wait_for_child()
        if proc is None:
            return  # nothing running; the worker sees the flag and starts no further step
        steps = ((signal.SIGINT, "log_stop_sigint", self._stop_grace),
                 (signal.SIGTERM, "log_stop_sigterm", self._term_grace),
                 (signal.SIGKILL, "log_stop_sigkill", None))
        for sig, key, grace in steps:
            with self._lock:
                if self._proc is not proc or self._exited.is_set():
                    return
                self.events.put(RunnerNotice("warn", key, {}))
                self._signal(proc, sig)
            if grace is None or self._exited.wait(grace):
                return

    def _wait_for_child(self) -> Any:
        # A stop can arrive while a step is being spawned; wait for the child
        # rather than missing it. Returns None once the worker is done.
        while True:
            with self._lock:
                if not self._busy:
                    return None
                if self._proc is not None and not self._exited.is_set():
                    return self._proc
            time.sleep(0.05)


def _unlink(name: str) -> None:
    try:
        os.unlink(name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        LOG.warning("could not remove temporary file %s: %s", name, exc)
