"""Narrow experimental client for the app's primary UUID profile.

Queries, recording controls, single-file deletion and clock setting are exposed.
OTA, reset and disk formatting are deliberately absent.
Validated on one recorder, not all models.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Callable

from bleak import BleakClient

from .framing import Frame, FrameDecoder, encode_request

SERVICE = "0011200a-2233-4455-6677-8899dfdedddc"
CONTROL_WRITE = "0011202a-2233-4455-6677-8899dfdedddc"
CONTROL_NOTIFY = "0011203a-2233-4455-6677-8899dfdedddc"
ARCHIVE_NOTIFY = "0011204a-2233-4455-6677-8899dfdedddc"


def _validate_recording_id(value: str) -> bytes:
    if len(value) != 14 or not value.isascii() or not value.isdigit():
        raise ValueError("recording ID must be 14 ASCII digits")
    datetime.strptime(value, "%Y%m%d%H%M%S")
    return value.encode("ascii")


class ProtocolError(RuntimeError):
    """The recorder returned a malformed or unexpected response."""


class ActiveRecordingError(ProtocolError):
    """An archive download requires the active/paused recording to be stopped."""


@dataclass(frozen=True, slots=True)
class RecordingStatus:
    state: str  # "recording", "paused", or "stopped"
    recording_id: str | None = None


@dataclass(frozen=True, slots=True)
class StorageInfo:
    """Device-reported megabytes; app displays these divided by 1000 as GB.

    These are coarse firmware values, not independently measured byte counts.
    """

    total_mb: int
    free_mb: int

    @property
    def used_mb(self) -> int:
        return self.total_mb - self.free_mb


@dataclass(frozen=True, slots=True)
class Recording:
    recording_id: str
    metadata: bytes

    @property
    def size_bytes(self) -> int:
        """Four-byte big-endian file size, as decoded by the Android app."""
        return int.from_bytes(self.metadata, "big")

    @property
    def recorded_at(self) -> datetime:
        """Device-local timestamp; no timezone is implied."""
        return datetime.strptime(self.recording_id, "%Y%m%d%H%M%S")

    @classmethod
    def from_frame(cls, frame: Frame) -> "Recording":
        if frame.command != 5 or len(frame.payload) != 18:
            raise ProtocolError("invalid catalog row")
        try:
            stamp = frame.payload[:14].decode("ascii")
            if not stamp.isascii() or not stamp.isdigit():
                raise ValueError("timestamp is not ASCII digits")
            datetime.strptime(stamp, "%Y%m%d%H%M%S")
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("invalid catalog timestamp") from exc
        return cls(stamp, frame.payload[14:])


class AirecClient:
    """Use as an async context manager; timeout never means an empty catalog.

    Pass a discovered Bleak BLEDevice where possible. Operations are serialized.
    A disconnected client does not reconnect implicitly. The MAC response is
    retained as raw bytes until its format is verified on actual hardware.
    """

    def __init__(self, device, *, timeout: float = 10.0,
                 trace: Callable[[str, bytes], None] | None = None,
                 client_factory=BleakClient):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.timeout = timeout
        self.trace = trace
        self._client = client_factory(device, disconnected_callback=self._disconnected)
        self._decoder = FrameDecoder()
        self._lock = asyncio.Lock()
        self._responses: asyncio.Queue[Frame | bytes | None] = asyncio.Queue(maxsize=4096)
        self._ready = False
        self._overflow = False
        self._expected: set[int] = set()
        self._bulk_subscribed = False
        self._download_active = False
        self._control_write_limit = 20
        self.mac_response: bytes | None = None

    def _disconnected(self, _client):
        self._ready = False
        if not self._responses.full():
            self._responses.put_nowait(None)

    def _notification(self, _characteristic, data):
        raw = bytes(data)
        if self.trace:
            self.trace("receive", raw)
        for frame in self._decoder.feed(raw):
            if frame.command not in self._expected:
                continue
            if self._responses.full():
                self._overflow = True
                break
            self._responses.put_nowait(frame)

    def _audio_notification(self, _characteristic, data):
        # Audio is never sent to the control trace callback.
        if not self._download_active:
            return
        if self._responses.full():
            self._overflow = True
        else:
            self._responses.put_nowait(bytes(data))

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, _exc, _traceback):
        try:
            await self.disconnect()
        except Exception:
            if exc_type is None:
                raise
            logging.getLogger(__name__).warning("Disconnect failed during error cleanup", exc_info=True)

    async def connect(self):
        async with self._lock:
            if self._ready:
                return
            if self._client.is_connected:
                await self._client.disconnect()
            self.mac_response = None
            self._bulk_subscribed = False
            self._download_active = False
            self._decoder.reset()
            self._overflow = False
            self._drain()
            try:
                await self._client.connect()
                service = self._client.services.get_service(SERVICE)
                if service is None:
                    raise ProtocolError("primary AIREC service is missing")
                write = service.get_characteristic(CONTROL_WRITE)
                notify = service.get_characteristic(CONTROL_NOTIFY)
                if write is None or "write-without-response" not in write.properties:
                    raise ProtocolError("expected control write characteristic is missing")
                if notify is None or "notify" not in notify.properties:
                    raise ProtocolError("expected control notification characteristic is missing")
                self._control_write_limit = write.max_write_without_response_size
                await self._client.start_notify(notify, self._notification)
                # The app delays getMac after MTU/PHY setup. See analysis notes.
                await asyncio.sleep(0.2)
                self._expected = {1}
                async with asyncio.timeout(self.timeout):
                    await self._write(1)
                    frame = await self._receive(1)
                if not frame.payload:
                    raise ProtocolError("MAC query returned no identity payload")
                self.mac_response = frame.payload
                self._ready = True
            except BaseException:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass  # Preserve the initialization failure.
                raise
            finally:
                self._expected.clear()

    async def disconnect(self):
        async with self._lock:
            self._ready = False
            self.mac_response = None
            self._expected.clear()
            self._download_active = False
            await self._client.disconnect()

    def _drain(self):
        while not self._responses.empty():
            self._responses.get_nowait()

    async def _write(self, command: int, payload: bytes = b""):
        if not self._client.is_connected:
            raise ConnectionError("recorder disconnected")
        if command == 7:
            if len(payload) != 18 or payload[14:] != b"\0" * 4:
                raise ValueError("download requires a timestamp and zero offset")
            _validate_recording_id(payload[:14].decode("ascii"))
        elif command in (2, 10):
            _validate_recording_id(payload.decode("ascii"))
        elif command == 33:
            if payload != b"R":
                raise ValueError("only ordinary recording mode is supported")
        elif command not in (1, 3, 4, 5, 8, 11, 14, 15, 16, 48) or payload:
            raise ValueError("command is not an allowed query or download operation")
        data = encode_request(command, payload)
        if self.trace:
            self.trace("send", data)
        characteristic = self._client.services.get_service(SERVICE).get_characteristic(CONTROL_WRITE)
        limit = max(characteristic.max_write_without_response_size, self._control_write_limit)
        if limit <= 0:
            raise ProtocolError("invalid control write payload limit")
        # The app splits logical requests to MTU-3. A download request is 22
        # bytes, exceeding the default 20-byte BLE payload on some hosts.
        for offset in range(0, len(data), limit):
            chunk = data[offset:offset + limit]
            if self.trace and len(data) > limit:
                self.trace("send_chunk", chunk)
            await self._client.write_gatt_char(CONTROL_WRITE, chunk, response=False)

    async def _prepare_download_transport(self):
        """Isolate Bleak 0.22's private Linux MTU workaround in one place."""
        backend = getattr(self._client, "_backend", None)
        if backend is not None and type(backend).__module__ == "bleak.backends.bluezdbus.client":
            # BlueZ's MaxWriteWithoutResponse property can stay at 20 even after
            # AcquireWrite returns MTU 247. Do not rely on the default MTU=23
            # reporting property. This is version-pinned in pyproject.toml.
            await asyncio.wait_for(backend._acquire_mtu(), timeout=self.timeout)
            self._control_write_limit = backend._mtu_size - 3

    @staticmethod
    def _status_code(frame: Frame) -> int:
        if frame.payload in (b"\x01", b"\x02"):
            return frame.payload[0]
        if len(frame.payload) == 15 and frame.payload[0] == 0:
            try:
                _validate_recording_id(frame.payload[1:].decode("ascii"))
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError("invalid active recording timestamp") from exc
            return 0
        raise ProtocolError("unrecognized live recording status")

    async def _receive(self, *commands: int) -> Frame:
        while True:
            if self._overflow:
                raise ProtocolError("control queue overflow; response completeness is unknown")
            if not self._client.is_connected:
                raise ConnectionError("recorder disconnected")
            frame = await self._responses.get()
            if frame is None:
                raise ConnectionError("recorder disconnected")
            if isinstance(frame, Frame) and frame.command in commands:
                return frame

    def _require_ready(self):
        if not self._ready or not self._client.is_connected:
            raise ConnectionError("connect and complete initialization first")

    @asynccontextmanager
    async def _operation(self):
        """Serialize controls; never retry a possibly executed mutation."""
        async with self._lock:
            self._require_ready()
            self._drain()
            try:
                async with asyncio.timeout(self.timeout):
                    yield
            except BaseException:
                self._ready = False
                raise
            finally:
                self._expected.clear()

    async def _exchange(self, command, payload=b"", *, failure=None):
        self._expected = {command} if failure is None else {command, failure}
        await self._write(command, payload)
        frame = await self._receive(*self._expected)
        if frame.command == failure:
            raise ProtocolError(f"recorder rejected command 0x{command:02x}")
        return frame

    async def _recording_status(self) -> RecordingStatus:
        frame = await self._exchange(15)
        code = self._status_code(frame)
        return RecordingStatus(("recording", "paused", "stopped")[code],
                               frame.payload[1:].decode("ascii") if code == 0 else None)

    async def recording_status(self) -> RecordingStatus:
        async with self._operation():
            return await self._recording_status()

    async def storage(self) -> StorageInfo:
        """Read total and free storage, requiring both replies in either order.

        No partial result is returned on timeout, malformed data or device error.
        This does not pause/stop recording, delete files or format storage.
        """
        async with self._operation():
            self._expected = {12, 13, 251}
            await self._write(11)
            values = {}
            while len(values) < 2:
                frame = await self._receive(12, 13, 251)
                if frame.command == 251:
                    raise ProtocolError("recorder rejected the storage query (0xfb)")
                if len(frame.payload) not in (2, 4):
                    raise ProtocolError("storage value must contain two or four bytes")
                value = int.from_bytes(frame.payload, "big")
                if frame.command in values and values[frame.command] != value:
                    raise ProtocolError("conflicting duplicate storage response")
                values[frame.command] = value
            if values[12] > values[13]:
                raise ProtocolError("reported free storage exceeds total storage")
            return StorageInfo(total_mb=values[13], free_mb=values[12])

    @staticmethod
    def _empty_ack(frame):
        if frame.payload:
            raise ProtocolError("unexpected acknowledgement payload")

    @staticmethod
    def _start_ack(frame):
        if frame.payload:
            try:
                _validate_recording_id(frame.payload.decode("ascii"))
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError("invalid recording start acknowledgement") from exc

    async def start_recording(self) -> RecordingStatus:
        """Start ordinary recording from stopped state; already active is a no-op.

        A paused recording must be resumed explicitly. Recording continues after
        this operation; connection closure behavior remains firmware-dependent.
        """
        async with self._operation():
            status = await self._recording_status()
            if status.state == "recording":
                return status
            if status.state == "paused":
                raise ActiveRecordingError("recording is paused; use resume_recording()")
            self._start_ack(await self._exchange(3))
            await asyncio.sleep(0.1)  # App sends ordinary-mode 0x21 after 100 ms.
            # This firmware does not acknowledge the app's mode hint. Confirm
            # the actual recording state instead of waiting for a 0x21 reply.
            await self._write(33, b"R")
            status = await self._recording_status()
            if status.state != "recording":
                raise ProtocolError("recorder did not confirm recording start")
            return status

    async def _set_paused(self, paused: bool) -> RecordingStatus:
        async with self._operation():
            status = await self._recording_status()
            desired = "paused" if paused else "recording"
            if status.state == desired:
                return status
            if status.state == "stopped":
                raise ProtocolError("no current recording; use start_recording()")
            self._empty_ack(await self._exchange(16, failure=255))
            result = await self._recording_status()
            if result.state != desired:
                raise ProtocolError("recorder did not confirm pause/resume transition")
            return result

    async def pause_recording(self) -> RecordingStatus:
        """Pause only after confirming active state; never blindly toggle."""
        return await self._set_paused(True)

    async def resume_recording(self) -> RecordingStatus:
        """Resume only after confirming paused state; active is a no-op."""
        return await self._set_paused(False)

    async def stop_recording(self) -> Recording | None:
        """Finalize the active/paused recording; stopped is a no-op."""
        async with self._operation():
            if (await self._recording_status()).state == "stopped":
                return None
            frame = await self._exchange(4)
            recording = Recording.from_frame(Frame(5, frame.payload))
            if (await self._recording_status()).state != "stopped":
                raise ProtocolError("recorder did not confirm stopped state")
            return recording

    async def _catalog(self) -> tuple[Recording, ...]:
        self._expected = {5, 6}
        await self._write(5)
        rows = []
        while True:
            frame = await self._receive(5, 6)
            if frame.command == 6:
                self._empty_ack(frame)
                return tuple(rows)
            rows.append(Recording.from_frame(frame))

    async def delete_recording(self, recording_id: str) -> None:
        """Irreversibly delete exactly one catalog ID, then verify its absence.

        Requires stopped state. Never stops recording implicitly, deletes local
        files, retries a deletion, or exposes an erase-all/format command.
        """
        identity = _validate_recording_id(recording_id)
        async with self._operation():
            if (await self._recording_status()).state != "stopped":
                raise ActiveRecordingError("stop recording before deleting an archive")
            rows = await self._catalog()
            if sum(row.recording_id == recording_id for row in rows) != 1:
                raise ValueError("recording ID is absent or ambiguous in the catalog")
            self._empty_ack(await self._exchange(10, identity, failure=252))
            if any(row.recording_id == recording_id for row in await self._catalog()):
                raise ProtocolError("deleted recording is still present in the catalog")

    async def _clock(self) -> datetime:
        frame = await self._exchange(48)
        try:
            _validate_recording_id(frame.payload.decode("ascii"))
            return datetime.strptime(frame.payload.decode("ascii"), "%Y%m%d%H%M%S")
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("invalid recorder clock response") from exc

    async def clock(self) -> datetime:
        """Read device-local wall time (no timezone on the wire)."""
        async with self._operation():
            return await self._clock()

    async def set_clock(self, value: datetime | None = None) -> datetime:
        """Set naive device-local time, defaulting to computer local time.

        Requires stopped state to avoid changing an active recording's identity.
        Aware datetimes are rejected: convert explicitly to the desired wall time.
        Returns read-back time after checking it against the requested value.
        """
        if value is not None and (not isinstance(value, datetime) or value.tzinfo is not None):
            raise ValueError("clock must be a naive datetime in the device's local timezone")
        async with self._operation():
            if (await self._recording_status()).state != "stopped":
                raise ActiveRecordingError("stop recording before setting the clock")
            value = (datetime.now() if value is None else value).replace(microsecond=0)
            stamp = f"{value.year:04d}{value.month:02d}{value.day:02d}{value.hour:02d}{value.minute:02d}{value.second:02d}"
            self._empty_ack(await self._exchange(2, _validate_recording_id(stamp)))
            actual = await self._clock()
            if not 0 <= (actual - value).total_seconds() <= self.timeout + 2:
                raise ProtocolError("recorder clock read-back differs from requested time")
            return actual

    async def battery(self) -> int:
        async with self._lock:
            self._require_ready()
            self._drain()
            self._expected = {14}
            try:
                async with asyncio.timeout(self.timeout):
                    await self._write(14)
                    frame = await self._receive(14)
                if len(frame.payload) != 1 or frame.payload[0] > 100:
                    raise ProtocolError("invalid battery percentage")
                return frame.payload[0]
            except BaseException:
                self._ready = False  # Reconnect before retrying; late replies are ambiguous.
                raise
            finally:
                self._expected.clear()

    async def download_recording(
        self, recording: Recording | str, *, timeout: float = 120.0,
        max_size: int = 128 * 1024 * 1024, stop_if_recording: bool = False,
    ) -> bytes:
        """Download one archive's raw audio, requiring size and end-marker checks.

        A string ID is resolved against a fresh catalog. A Recording supplies its
        catalog size directly. Data may arrive before the acknowledgement or
        after the end marker on the separate BLE channel; neither is discarded.
        Refuses an active/paused recording unless stop_if_recording=True. That
        option stops/finalizes the current recording after checking status and
        does not restart it. No clock synchronization, deletion or OTA is sent.
        On failure/cancellation, stop transfer and disconnect before any retry.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("download timeout must be finite and positive")
        if not isinstance(max_size, int) or max_size < 0:
            raise ValueError("max_size must be a nonnegative integer")
        if isinstance(recording, str):
            _validate_recording_id(recording)
            matches = [row for row in await self.list_recordings() if row.recording_id == recording]
            if len(matches) != 1:
                raise ValueError("recording ID is absent or ambiguous in the catalog")
            recording = matches[0]
        identity = _validate_recording_id(recording.recording_id)
        if len(recording.metadata) != 4:
            raise ValueError("recording requires four-byte catalog size metadata")
        expected_size = recording.size_bytes
        if expected_size > max_size:
            raise ValueError(f"recording exceeds download limit of {max_size} bytes")
        async with self._lock:
            self._require_ready()
            self._drain()
            self._expected = {15, 4}
            started = False
            try:
                async with asyncio.timeout(timeout):
                    async with asyncio.timeout(self.timeout):
                        await self._write(15)
                        status_code = self._status_code(await self._receive(15))
                    if status_code in (0, 1):
                        if not stop_if_recording:
                            raise ActiveRecordingError("recorder has an active/paused recording; stop it or set stop_if_recording=True")
                        async with asyncio.timeout(self.timeout):
                            await self._write(4)
                            stopped = await self._receive(4)
                            if len(stopped.payload) != 18:
                                raise ProtocolError("unexpected stop acknowledgement")
                            _validate_recording_id(stopped.payload[:14].decode("ascii"))
                            await self._write(15)
                            if self._status_code(await self._receive(15)) != 2:
                                raise ProtocolError("recorder did not confirm stopped status")
                    await self._prepare_download_transport()
                    self._expected = {7, 9, 0xFD}
                    if not self._bulk_subscribed:
                        service = self._client.services.get_service(SERVICE)
                        bulk = service.get_characteristic(ARCHIVE_NOTIFY)
                        if bulk is None or "notify" not in bulk.properties:
                            raise ProtocolError("archive notification characteristic is missing")
                        await self._client.start_notify(bulk, self._audio_notification)
                        self._bulk_subscribed = True
                    self._download_active = True
                    started = True
                    await self._write(7, identity + b"\0" * 4)
                    acknowledged = False
                    completed = False
                    audio = bytearray()
                    while True:
                        if self._overflow:
                            raise ProtocolError("download queue overflow; audio is incomplete")
                        if not self._client.is_connected:
                            raise ConnectionError("recorder disconnected during download")
                        event = await self._responses.get()
                        if event is None:
                            raise ConnectionError("recorder disconnected during download")
                        if isinstance(event, bytes):
                            if len(audio) + len(event) > expected_size:
                                raise ProtocolError("audio exceeds catalog size")
                            audio.extend(event)
                        elif event.command == 0xFD:
                            raise ProtocolError("recorder rejected the download (0xfd)")
                        elif event.command == 7:
                            if len(event.payload) != 18 or event.payload[:14] != identity:
                                raise ProtocolError("download acknowledgement has wrong ID or shape")
                            size = int.from_bytes(event.payload[14:], "big")
                            if size != expected_size:
                                raise ProtocolError("download acknowledgement size differs from catalog")
                            acknowledged = True
                        elif event.command == 9:
                            if event.payload:
                                raise ProtocolError("unexpected download completion payload")
                            completed = True
                        if acknowledged and completed and len(audio) == expected_size and self._responses.empty():
                            if self._overflow:
                                raise ProtocolError("download queue overflow")
                            return bytes(audio)
            except BaseException:
                self._ready = False
                self._download_active = False
                if started and self._client.is_connected:
                    try:
                        await asyncio.wait_for(self._write(8), timeout=min(self.timeout, 2.0))
                    except Exception:
                        logging.getLogger(__name__).warning("Unable to stop failed transfer", exc_info=True)
                try:
                    await self._client.disconnect()
                except Exception:
                    logging.getLogger(__name__).warning("Download failure cleanup could not disconnect", exc_info=True)
                raise
            finally:
                self._download_active = False
                self._expected.clear()

    async def list_recordings(self) -> tuple[Recording, ...]:
        """Collect rows through the explicit end marker, without a pause toggle.

        No partial result is returned on timeout. No count is known, so the end
        marker is the only completeness evidence. Firmware may require further
        initialization on other models; this path remains experimental.
        """
        async with self._operation():
            return await self._catalog()
