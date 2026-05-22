"""Best-effort macOS FSEvents adapter with safe full-scan fallback."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path
import sys
import time


_MUST_SCAN_SUBDIRS = 0x00000001
_USER_DROPPED = 0x00000002
_KERNEL_DROPPED = 0x00000004
_EVENT_IDS_WRAPPED = 0x00000008
_HISTORY_DONE = 0x00000010
_ROOT_CHANGED = 0x00000020
_K_CFSTRING_ENCODING_UTF8 = 0x08000100


@dataclass(frozen=True)
class FSEventChanges:
    """Filesystem changes since a stored FSEvents event id."""

    supported: bool
    current_event_id: int | None
    changed_paths: tuple[Path, ...] = ()
    must_full_scan: bool = False


class MacFSEvents:
    """Small ctypes wrapper around macOS FSEvents.

    The wrapper is intentionally conservative: any setup, stream, or callback
    error reports ``must_full_scan`` so the normal safe scanner remains the
    source of truth.
    """

    def __init__(self, *, timeout: float = 0.35) -> None:
        self.timeout = timeout
        self._core_services = None
        self._core_foundation = None

    def current_event_id(self) -> int | None:
        if sys.platform != "darwin":
            return None
        try:
            core_services, _core_foundation = self._libraries()
            core_services.FSEventsGetCurrentEventId.restype = ctypes.c_uint64
            return int(core_services.FSEventsGetCurrentEventId())
        except Exception:
            return None

    def changes_since(self, roots: tuple[Path, ...], since_event_id: int | None) -> FSEventChanges:
        current_event_id = self.current_event_id()
        if sys.platform != "darwin" or current_event_id is None or since_event_id is None:
            return FSEventChanges(supported=False, current_event_id=current_event_id, must_full_scan=True)
        if current_event_id <= since_event_id:
            return FSEventChanges(supported=True, current_event_id=current_event_id)

        try:
            return self._stream_changes(roots, since_event_id, current_event_id)
        except Exception:
            return FSEventChanges(supported=True, current_event_id=current_event_id, must_full_scan=True)

    def _libraries(self):
        if self._core_services is None:
            self._core_services = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        if self._core_foundation is None:
            self._core_foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        return self._core_services, self._core_foundation

    def _stream_changes(
        self,
        roots: tuple[Path, ...],
        since_event_id: int,
        current_event_id: int,
    ) -> FSEventChanges:
        core_services, core_foundation = self._libraries()
        callback_type = ctypes.CFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
        )

        changed_paths: set[Path] = set()
        must_full_scan = False
        history_done = False

        def callback(_stream, _info, num_events, event_paths, event_flags, _event_ids):
            nonlocal must_full_scan, history_done
            paths = ctypes.cast(event_paths, ctypes.POINTER(ctypes.c_char_p))
            for index in range(num_events):
                flags = int(event_flags[index])
                if flags & (_USER_DROPPED | _KERNEL_DROPPED | _EVENT_IDS_WRAPPED | _ROOT_CHANGED):
                    must_full_scan = True
                if flags & _HISTORY_DONE:
                    history_done = True
                    continue
                raw_path = paths[index]
                if raw_path:
                    changed_paths.add(Path(raw_path.decode("utf-8", errors="surrogateescape")))
                if flags & _MUST_SCAN_SUBDIRS and raw_path:
                    changed_paths.add(Path(raw_path.decode("utf-8", errors="surrogateescape")))

        callback_ref = callback_type(callback)
        paths_array, path_strings = self._create_paths_array(core_foundation, roots)
        stream = None
        try:
            core_foundation.CFRunLoopGetCurrent.restype = ctypes.c_void_p
            core_foundation.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
            run_loop = core_foundation.CFRunLoopGetCurrent()
            default_mode = ctypes.c_void_p.in_dll(core_foundation, "kCFRunLoopDefaultMode")
            core_services.FSEventStreamCreate.restype = ctypes.c_void_p
            core_services.FSEventStreamCreate.argtypes = [
                ctypes.c_void_p,
                callback_type,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint64,
                ctypes.c_double,
                ctypes.c_uint32,
            ]
            core_services.FSEventStreamScheduleWithRunLoop.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            core_services.FSEventStreamStart.argtypes = [ctypes.c_void_p]
            core_services.FSEventStreamStart.restype = ctypes.c_bool
            core_services.FSEventStreamFlushSync.argtypes = [ctypes.c_void_p]
            core_services.FSEventStreamStop.argtypes = [ctypes.c_void_p]
            core_services.FSEventStreamInvalidate.argtypes = [ctypes.c_void_p]
            core_services.FSEventStreamRelease.argtypes = [ctypes.c_void_p]
            stream = core_services.FSEventStreamCreate(
                None,
                callback_ref,
                None,
                paths_array,
                ctypes.c_uint64(since_event_id),
                ctypes.c_double(0.1),
                ctypes.c_uint32(0),
            )
            if not stream:
                return FSEventChanges(supported=True, current_event_id=current_event_id, must_full_scan=True)

            core_services.FSEventStreamScheduleWithRunLoop(stream, run_loop, default_mode)
            if not core_services.FSEventStreamStart(stream):
                return FSEventChanges(supported=True, current_event_id=current_event_id, must_full_scan=True)
            core_services.FSEventStreamFlushSync(stream)

            deadline = time.monotonic() + self.timeout
            while not history_done and time.monotonic() < deadline:
                core_foundation.CFRunLoopRunInMode(default_mode, 0.05, True)

            return FSEventChanges(
                supported=True,
                current_event_id=current_event_id,
                changed_paths=tuple(sorted(changed_paths)),
                must_full_scan=must_full_scan,
            )
        finally:
            if stream:
                core_services.FSEventStreamStop(stream)
                core_services.FSEventStreamInvalidate(stream)
                core_services.FSEventStreamRelease(stream)
            for path_string in path_strings:
                core_foundation.CFRelease(path_string)
            core_foundation.CFRelease(paths_array)

    def _create_paths_array(self, core_foundation, roots: tuple[Path, ...]):
        core_foundation.CFArrayCreateMutable.restype = ctypes.c_void_p
        core_foundation.CFArrayCreateMutable.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        core_foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        core_foundation.CFArrayAppendValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        core_foundation.CFRelease.argtypes = [ctypes.c_void_p]

        paths_array = core_foundation.CFArrayCreateMutable(None, 0, None)
        path_strings: list[int] = []
        for root in roots:
            path_string = core_foundation.CFStringCreateWithCString(
                None,
                str(root).encode("utf-8", errors="surrogateescape"),
                _K_CFSTRING_ENCODING_UTF8,
            )
            if path_string:
                core_foundation.CFArrayAppendValue(paths_array, path_string)
                path_strings.append(path_string)
        return paths_array, path_strings
