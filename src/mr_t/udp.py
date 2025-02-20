from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class UdpPing:
    pass


@dataclass(frozen=True)
class UdpPong:
    series_id: int


UdpRequest: TypeAlias = UdpPing

UdpReply: TypeAlias = UdpPong
