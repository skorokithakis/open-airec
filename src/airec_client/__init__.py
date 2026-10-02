"""Experimental AIREC client validated on one recorder's primary BLE profile."""

from .framing import Frame, FrameDecoder, encode_request
from .client import ActiveRecordingError, AirecClient, ProtocolError, Recording, RecordingStatus, StorageInfo
from .audio import save_audio, to_ogg_opus

__all__ = ["ActiveRecordingError", "AirecClient", "ProtocolError", "Recording", "RecordingStatus", "StorageInfo", "Frame",
           "FrameDecoder", "encode_request", "save_audio", "to_ogg_opus"]
