"""Serialized queries, one coalesced manual request, bounded retry cadence."""
from __future__ import annotations
import logging
import threading
import time
import uuid
from PySide6.QtCore import QThread, Signal
from atk_query import TargetQuery
from data_store import write_sample, write_query_event

log = logging.getLogger(__name__)

class BatteryWorker(QThread):
    result = Signal(object)
    checking = Signal(object)

    def __init__(self, interval=300, query=None, writer=None, event_writer=None):
        super().__init__()
        self.interval = max(30, int(interval))
        self.query = query or TargetQuery()
        self.writer = writer or write_sample
        self.event_writer = event_writer or write_query_event
        self._cv = threading.Condition()
        self._stopping = False
        self._pending_manual = None
        self._cancel = None
        self._generation = 0
        self._counter = 0
        self._session = uuid.uuid4().hex[:12]
        self._due = 0.0
        self._last_success = None
        self._failures = 0
        self._storage_error = None

    def _new_id(self, kind="q"):
        self._counter += 1
        return f"{self._session}-{kind}{self._counter:06d}"

    def refresh(self):
        with self._cv:
            if self._stopping:
                return None
            if self._pending_manual is None:
                self._pending_manual = self._new_id("r")
            token = self._pending_manual
            self._cv.notify_all()
            return token

    def set_interval(self, seconds):
        with self._cv:
            self.interval = max(30, int(seconds))
            self._due = min(self._due, time.monotonic() + self.interval)
            self._cv.notify_all()

    def resume(self):
        """Invalidate in-flight work, then query once without catching up."""
        with self._cv:
            self._generation += 1
            if self._cancel:
                self._cancel.set()
            self._due = 0.0
            self._cv.notify_all()

    def request_stop(self):
        with self._cv:
            self._stopping = True
            self._generation += 1
            self._pending_manual = None
            if self._cancel:
                self._cancel.set()
            self._cv.notify_all()

    def finish(self):
        self.request_stop()
        return self.wait(1800)

    @staticmethod
    def failure_delay(failures, outcome):
        if outcome == "receiver_absent":
            return 30
        return 15 if failures == 1 else 60 if failures == 2 else 300

    def run(self):
        while True:
            with self._cv:
                while not self._stopping and self._pending_manual is None and time.monotonic() < self._due:
                    self._cv.wait(min(1.0, max(0, self._due - time.monotonic())))
                if self._stopping:
                    return
                token = self._pending_manual
                self._pending_manual = None
                manual = token is not None
                query_id = self._new_id()
                generation = self._generation
                cancel = self._cancel = threading.Event()
            self.checking.emit(dict(query_id=query_id, request_id=token, manual=manual))
            event = self.query(query_id, "manual" if manual else "auto", cancel)
            event["request_id"] = token
            with self._cv:
                if self._stopping:
                    return
                if generation != self._generation:
                    if manual and self._pending_manual is None:
                        self._pending_manual = token
                    continue
            # Filesystem I/O must not hold the scheduling lock: a slow disk must
            # not block refresh()/request_stop() on the GUI thread.
            event["consecutive_failures"] = 0 if event["connected"] else self._failures + 1
            event["failures_before"] = self._failures
            event["recovered"] = bool(event["connected"] and self._failures)
            event["storage_error"] = self._storage_error
            event["storage_attempted"] = bool(event["connected"])
            if event["connected"]:
                self._last_success = {k: event.get(k) for k in
                    ("at", "percent", "voltage_mv", "voltage_raw", "device_id", "device_uid",
                     "vid_pid", "model_id", "model_display_name", "lut_profile_id",
                     "binding_state", "protocol_family", "link_mode", "link_byte", "query_id")}
                try:
                    self.writer(event)
                    self._storage_error = None
                    event["storage_error"] = None
                except Exception as exc:
                    self._storage_error = event["storage_error"] = f"{type(exc).__name__}: {exc}"
                    log.exception("query=%s valid reading could not be stored", query_id)
            self._failures = event["consecutive_failures"]
            event["last_success"] = self._last_success
            event["next_delay_s"] = self.interval if event["connected"] else self.failure_delay(self._failures, event["outcome"])
            try:
                self.event_writer(event)
            except Exception as exc:
                event["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
                log.exception("query=%s diagnostic event could not be stored", query_id)
            with self._cv:
                if self._stopping:
                    return
                if generation != self._generation:
                    if manual and self._pending_manual is None:
                        self._pending_manual = token
                    continue
                self._due = time.monotonic() + event["next_delay_s"]
                self.result.emit(event)
