"""Experimental AIREC client validated on one recorder's primary BLE profile."""

from .framing import Frame, FrameDecoder, encode_request
from .client import (ActiveRecordingError, AirecClient, ChipInfo, DeviceSettings,
                     DownloadInterrupted, ProtocolError, Recording, RecordingStatus,
                     StorageInfo)
from .audio import save_audio, to_ogg_opus
from .scan import Recorder, find_recorders
from .sync import SyncEvent, SyncFailed, sync_directory

__all__ = ["ActiveRecordingError", "AirecClient", "ChipInfo", "DeviceSettings", "DownloadInterrupted",
           "ProtocolError", "Recording", "RecordingStatus", "StorageInfo", "Frame",
           "FrameDecoder", "encode_request", "save_audio", "to_ogg_opus", "Recorder", "find_recorders",
           "SyncEvent", "SyncFailed", "sync_directory"]
