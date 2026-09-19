from dataclasses import dataclass

@dataclass(frozen=True)
class Frame:
    topic: str
    payload: bytes
    received_at: float

@dataclass(frozen=True)
class Capture:
    data: bytes
    sha256: str
    timestamp: int
    kind: str
    attributes_missing: bool
