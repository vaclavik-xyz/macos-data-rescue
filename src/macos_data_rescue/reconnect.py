"""Wait -> cooldown -> remount -> identity check, after a storage loss.

Deterministic rules only (docs/superpowers/specs/2026-10-22-auto-reconnect-design.md):

- The source is only ever mounted with `diskutil mount readOnly`. A source that
  comes back writable or at another mount point gets a plain `diskutil unmount`
  (never forced) and a read-only mount; if that fails or it lands elsewhere
  again, the run stops for an operator decision. Paths are never rerooted.
- The destination is only mounted when present and unmounted, never unmounted.
- Network volumes, volumes without a recorded UUID, and volumes recorded outside
  /Volumes are only waited for.
- Every stop records `gave-up`; the persisted limits in storage_events decide
  when repeated losses become an imaging/escalation recommendation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .storage import StoragePaused
from .storage_events import ReconnectLimits, events_since_acknowledgement, limit_breach, record_event
from .volumes import Diskutil

ESCALATION_HINT = "stop file-level copying and image the disk (ddrescue) or escalate; see docs/agent-runbook.md"
OPERATOR_HINT = "operator decision required"
# Only volumes auto-mounted under /Volumes are remounted. The boot/data volume
# and custom mount points are never unmounted by the tool.
MANAGED_MOUNT_ROOT = "/Volumes/"
ROLES = ("source", "destination")


class ReconnectGaveUp(Exception):
    """Operator-facing reason why the reconnect loop stopped."""


@dataclass(frozen=True)
class ReconnectPolicy:
    cooldown: float = 300.0
    wait: float = 1800.0
    poll: float = 15.0
    limits: ReconnectLimits = field(default_factory=ReconnectLimits)


def is_managed(identity: dict | None) -> bool:
    if not identity or not identity.get("uuid") or identity.get("network"):
        return False
    mount_point = identity.get("mount_point")
    return isinstance(mount_point, str) and mount_point.startswith(MANAGED_MOUNT_ROOT)


class Reconnector:
    def __init__(self, conn, identities: dict, probe: Callable[[], None], policy: ReconnectPolicy,
                 diskutil: Diskutil, *, on_wait: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self.conn = conn
        self.identities = identities
        self.probe = probe
        self.policy = policy
        self.diskutil = diskutil
        self.on_wait = on_wait
        self.sleep = sleep
        self.clock = clock

    def recover(self, reason: str, bytes_since_resume: int) -> None:
        """Return once storage is back and verified; raise ReconnectGaveUp otherwise."""
        now = self.clock()
        record_event(self.conn, "lost", detail={"reason": reason, "bytes_since_resume": int(bytes_since_resume)},
                     now=now)
        breach = limit_breach(self.conn, self.policy.limits, now)
        if breach:
            self._give_up(breach, escalate=True)
        attempt = sum(1 for event in events_since_acknowledgement(self.conn) if event["kind"] == "lost")
        until = now + self.policy.cooldown
        record_event(self.conn, "cooldown", detail={"seconds": self.policy.cooldown, "attempt": attempt}, now=now)
        self._notify("cooldown", until, attempt, reason)
        self._sleep_until(until)
        deadline = self.clock() + self.policy.wait
        self._notify("waiting", deadline, attempt, reason)
        mounted = False
        last_error = reason
        stale = False
        while True:
            listed = []
            for role in ROLES:
                identity = self.identities.get(role)
                if is_managed(identity):
                    did_mount, is_listed = self._restore_role(role, identity)
                    mounted = mounted or did_mount
                    listed.append(is_listed)
                else:
                    listed.append(False)
            try:
                self.probe()
            except StoragePaused as exc:
                last_error = str(exc)
                # every role still listed at its recorded mount point, yet I/O fails
                stale = all(listed)
            else:
                if self._identity_confirmed():
                    if mounted:
                        record_event(self.conn, "remounted", detail={"attempt": attempt}, now=self.clock())
                    record_event(self.conn, "resumed", detail={"attempt": attempt}, now=self.clock())
                    return
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            self.sleep(min(self.policy.poll, remaining))
        message = f"storage did not come back within {self.policy.wait:g} seconds ({last_error})"
        if stale:
            message += "; stale mount, reboot or replug"
        self._give_up(message, escalate=True)

    def _restore_role(self, role: str, identity: dict) -> tuple[bool, bool]:
        """Return (mounted now, listed at the recorded mount point)."""
        uuid, recorded = identity["uuid"], identity["mount_point"]
        info = self.diskutil.info(uuid)
        if info is None:
            return False, False  # absent or diskutil timed out: not available yet
        if info.uuid is not None and info.uuid != uuid:
            self._give_up(f"{role} lookup for volume {uuid} returned volume {info.uuid}", escalate=False)
        if info.locked:
            self._give_up(f"{role} volume {uuid} needs unlocking", escalate=False)
        if info.mount_point is None:
            return self._mount(role, uuid, recorded), False
        if info.mount_point != recorded:
            if role != "source":
                self._give_up(f"destination volume is mounted at {info.mount_point} instead of {recorded}; "
                              "the destination is never unmounted automatically", escalate=False)
            return self._remount_source(uuid, recorded, f"mounted at {info.mount_point} instead of {recorded}"), False
        if role == "source" and info.writable is True:
            return self._remount_source(uuid, recorded, "mounted writable"), False
        return False, True

    def _remount_source(self, uuid: str, recorded: str, why: str) -> bool:
        record_event(self.conn, "remount-attempt", role="source",
                     detail={"action": "unmount", "uuid": uuid, "why": why}, now=self.clock())
        if not self.diskutil.unmount(uuid):
            self._give_up(f"source volume is {why} and a plain unmount failed", escalate=False)
        return self._mount("source", uuid, recorded)

    def _mount(self, role: str, uuid: str, recorded: str) -> bool:
        read_only = role == "source"
        record_event(self.conn, "remount-attempt", role=role,
                     detail={"action": "mount", "uuid": uuid, "read_only": read_only}, now=self.clock())
        if not self.diskutil.mount(uuid, read_only=read_only):
            return False
        after = self.diskutil.info(uuid)
        if after is not None and after.mount_point is not None:
            if after.mount_point != recorded:
                self._give_up(f"{role} volume was mounted at {after.mount_point} instead of {recorded}",
                              escalate=False)
            if read_only and after.writable is True:
                self._give_up("source volume came back writable despite a read-only mount request",
                              escalate=False)
        return True

    def _identity_confirmed(self) -> bool:
        for role in ROLES:
            identity = self.identities.get(role)
            if not is_managed(identity):
                continue
            info = self.diskutil.info(identity["mount_point"])
            if info is None or info.mount_point != identity["mount_point"]:
                return False
            if info.uuid != identity["uuid"]:
                self._give_up(f"a different volume ({info.uuid}) is mounted at {identity['mount_point']} "
                              f"instead of the recorded {role} volume {identity['uuid']}", escalate=False)
        return True

    def _give_up(self, reason: str, *, escalate: bool) -> None:
        message = f"{reason}; {ESCALATION_HINT if escalate else OPERATOR_HINT}"
        record_event(self.conn, "gave-up", detail={"reason": message}, now=self.clock())
        raise ReconnectGaveUp(message)

    def _notify(self, phase: str, until: float, attempt: int, reason: str) -> None:
        if self.on_wait is not None:
            self.on_wait({"phase": phase, "until": until, "attempt": attempt, "reason": reason})

    def _sleep_until(self, until: float) -> None:
        while (remaining := until - self.clock()) > 0:
            self.sleep(min(self.policy.poll, remaining))
