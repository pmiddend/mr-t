from dataclasses import dataclass


@dataclass(frozen=True)
class UdpPing:
    pass


@dataclass(frozen=True)
class UdpPong:
    series_id: int


type UdpRequest = UdpPing

type UdpReply = UdpPong
