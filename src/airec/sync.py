"""Bulk-sync recorder archives into a local directory.

Orchestrates one already-connected client: an explicit stop when the caller
permits it, exactly one catalog fetch, then per-recording download/publish with
resumable ``.part`` scratch files and optional delete-after-download. It adds no
retry/reconnect, concurrency or byte-level progress; the first failure stops the
run and raises :class:`SyncFailed` carrying everything reached beforehand.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .audio import save_audio
from .client import (ActiveRecordingError, AirecClient, DownloadInterrupted,
                     ProtocolError, Recording)

_EXTENSIONS = {"opus": ".opus", "raw": ".airec"}

# Measured BLE throughput is ~110-130 KB/s and a full 60-minute segment is about
# 14.4 MB (~130 s), so download_recording's 120 s default would time out on full
# segments. 600 s gives headroom; the CLI uses the same default.
_DEFAULT_DOWNLOAD_TIMEOUT = 600.0


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """One per-recording outcome, emitted in catalog order.

    ``action`` is ``skipped``, ``downloaded``, ``resumed``, ``deleted`` or
    ``failed``. ``bytes`` is the raw count transferred for this recording during
    this run (``None`` for skipped/deleted/failed). ``resumed_from`` is the
    nonzero offset a resumed transfer started from (``None`` otherwise).
    ``error`` carries a summary for ``failed`` events.
    """

    action: str
    recording_id: str
    bytes: int | None = None
    resumed_from: int | None = None
    error: str | None = None


class SyncFailed(RuntimeError):
    """Bulk sync stopped at the first failure.

    ``events`` holds every outcome reached, including the terminal ``failed``
    event, so a caller can report what was already done. The originating
    exception is chained as ``__cause__``.
    """

    def __init__(self, message: str, events: tuple[SyncEvent, ...]):
        super().__init__(message)
        self.events = tuple(events)


def _record(events: list[SyncEvent], progress: Callable[[SyncEvent], None] | None,
            event: SyncEvent) -> None:
    events.append(event)
    if progress is not None:
        progress(event)


def _append_partial(path: Path, data: bytes) -> None:
    """Append interrupted bytes durably: append, flush, fsync."""
    with open(path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _is_deletable_output(path: Path) -> bool:
    """Whether a pre-existing output file may back a ``delete_after`` deletion.

    Only a regular, non-empty file qualifies: a symlink (even to a regular file)
    or an empty file is treated as not saved, so the recording is left alone.
    """
    if path.is_symlink() or not path.is_file():
        return False
    return path.stat().st_size > 0


async def _delete_recording(
    client: AirecClient, recording_id: str, events: list[SyncEvent],
    progress: Callable[[SyncEvent], None] | None,
) -> None:
    """Delete one recording, emitting ``deleted`` or failing the run.

    Shared by the post-download and skipped-output deletion paths so both stop
    the run identically on failure.
    """
    try:
        await client.delete_recording(recording_id)
    except Exception as exc:
        _record(events, progress, SyncEvent(
            "failed", recording_id, error=f"{type(exc).__name__}: {exc}"))
        raise SyncFailed(
            f"sync stopped while deleting {recording_id}: {type(exc).__name__}: {exc}",
            events) from exc
    _record(events, progress, SyncEvent("deleted", recording_id))


def _resume_prefix(part: Path, size: int) -> tuple[bytes, int]:
    """Return ``(prefix, offset)`` from a scratch file, or ``(b"", 0)``.

    An existing scratch path that is not an ordinary regular file is refused
    rather than followed. A scratch file at least as large as the catalog entry
    cannot be valid, so it is discarded and the transfer restarts from zero.
    """
    if not (part.exists() or part.is_symlink()):
        return b"", 0
    if part.is_symlink() or not part.is_file():
        raise ValueError(f"partial scratch path is not a regular file: {part}")
    length = part.stat().st_size
    if length >= size:
        part.unlink()
        return b"", 0
    return part.read_bytes(), length


async def _download_with_resume(
    client: AirecClient, recording: Recording, part: Path, destination: Path, *,
    format: str = "opus", timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT,
    stop_if_recording: bool = False,
) -> tuple[int, int]:
    """Download one recording into ``destination``, resuming ``part`` first.

    Shared by the CLI ``download`` command and :func:`sync_directory`. Returns
    ``(bytes_transferred, resume_offset)`` where the count excludes bytes already
    held in ``part`` and the offset is ``0`` unless a valid scratch file was
    resumed. The ``part + new`` bytes must equal the catalog size before they are
    published with :func:`save_audio`; the scratch file is removed afterwards.

    ``part`` follows the ``<destination parent>/<ID>.part`` naming, so a download
    and a sync into the same directory share partial files. On
    :class:`DownloadInterrupted`, any nonempty ``partial`` is appended durably
    (append then fsync) before the exception is re-raised; if that write itself
    fails, the persistence error propagates naturally with the interruption as
    its context.
    """
    prefix, offset = _resume_prefix(part, recording.size_bytes)
    try:
        data = await client.download_recording(
            recording, offset=offset, timeout=timeout,
            stop_if_recording=stop_if_recording)
    except DownloadInterrupted as exc:
        # Persist only attributable bytes: .partial is empty without a matching
        # full-size acknowledgement.
        if exc.partial:
            _append_partial(part, exc.partial)
        raise
    raw = prefix + data
    if len(raw) != recording.size_bytes:
        raise ProtocolError(
            f"downloaded {len(raw)} bytes but catalog size is {recording.size_bytes}")
    save_audio(raw, destination, format=format)
    if part.exists() or part.is_symlink():
        part.unlink()
    return len(data), offset


async def sync_directory(
    client: AirecClient, directory: str | Path, *, format: str = "opus",
    stop_recording: bool = False, delete_after: bool = False,
    download_timeout: float = _DEFAULT_DOWNLOAD_TIMEOUT,
    progress: Callable[[SyncEvent], None] | None = None,
) -> tuple[SyncEvent, ...]:
    """Download every catalog recording missing from ``directory``.

    ``client`` must already be connected. The directory must exist. When the
    recorder is recording or paused, ``stop_recording`` is required and, if set,
    the active recording is finalized once through the existing stop path before
    the catalog is fetched, so the finalized entry is included. Each recording is
    skipped when its output file exists; otherwise it is downloaded (resuming an
    existing ``.part`` scratch file when shorter than the catalog size), checked
    against the catalog size, and published with :func:`save_audio`.
    ``download_timeout`` is the per-file transfer deadline passed to
    :meth:`AirecClient.download_recording`; the default covers a full 60-minute
    segment at measured BLE throughput. With ``delete_after``, every recording
    whose output file exists when it is reached is deleted from the recorder
    immediately afterwards -- both just-published files and pre-existing ones. A
    pre-existing output is deleted only when it is a regular, non-empty file and
    not a symlink; otherwise the recording is left alone. The first failure stops
    the run and raises :class:`SyncFailed`; rerunning continues from the
    remaining recordings.
    """
    if format not in _EXTENSIONS:
        raise ValueError("format must be 'opus' or 'raw'")
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(f"sync directory does not exist: {directory}")
    events: list[SyncEvent] = []

    status = await client.recording_status()
    if status.state != "stopped":
        if not stop_recording:
            raise ActiveRecordingError(
                "recorder is recording or paused; stop it or enable stop_recording")
        await client.stop_recording()

    recordings = await client.list_recordings()
    suffix = _EXTENSIONS[format]
    for recording in recordings:
        recording_id = recording.recording_id
        destination = directory / f"{recording_id}{suffix}"
        if destination.exists() or destination.is_symlink():
            _record(events, progress, SyncEvent("skipped", recording_id))
            if delete_after and _is_deletable_output(destination):
                await _delete_recording(client, recording_id, events, progress)
            continue

        part = directory / f"{recording_id}.part"
        try:
            transferred, offset = await _download_with_resume(
                client, recording, part, destination, format=format,
                timeout=download_timeout)
            action = "resumed" if offset else "downloaded"
            _record(events, progress, SyncEvent(action, recording_id, bytes=transferred,
                                                resumed_from=offset or None))
        except Exception as exc:
            _record(events, progress, SyncEvent(
                "failed", recording_id, error=f"{type(exc).__name__}: {exc}"))
            raise SyncFailed(
                f"sync stopped at {recording_id}: {type(exc).__name__}: {exc}",
                events) from exc

        if delete_after:
            await _delete_recording(client, recording_id, events, progress)

    return tuple(events)
