import asyncio
from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Any, AsyncIterable, AsyncIterator, TypeAlias, TypeVar

import asyncudp
import structlog
from tap import Tap

parent_log = structlog.get_logger()


class Arguments(Tap):
    udp_port: int


@dataclass(frozen=True)
class UdpPing:
    addr: Any


@dataclass(frozen=True)
class UdpPong:
    series_and_frame: None | tuple[int, int]


@dataclass(frozen=True)
class UdpPacketRequest:
    addr: Any
    frame_number: int
    start_byte: int


@dataclass(frozen=True)
class UdpPacketReply:
    frame_number: int
    start_byte: int
    bytes_in_frame: int
    payload: bytes


UdpRequest: TypeAlias = UdpPing | UdpPacketRequest
UdpReply: TypeAlias = UdpPong | UdpPacketReply


def decode_udp_request(b: bytes, addr: Any) -> None | UdpRequest:
    assert b
    match b[0]:
        case 0:
            return UdpPing(addr)
        case 2:
            frame_number, start_byte = struct.unpack(">II", b[1:])
            return UdpPacketRequest(addr, frame_number, start_byte)
        case _:
            parent_log.info(f"invalid message code {b[0]}")
            return None


def encode_udp_reply(r: UdpReply) -> bytes:  # type: ignore
    match r:  # type: ignore
        case UdpPong(series_and_frame=None):
            return struct.pack(">BII", 1, 0, 0)
        case UdpPong(series_and_frame=(series_id, frame_count)):
            return struct.pack(">BII", 1, series_id, frame_count)
        case UdpPacketReply(frame_number, start_byte, bytes_in_frame, payload):
            return (
                struct.pack(">BIII", 3, frame_number, start_byte, bytes_in_frame)
                + payload
            )


@dataclass(frozen=True)
class SeriesStart:
    series_id: int


@dataclass(frozen=True)
class SeriesEnd:
    pass


@dataclass(frozen=True)
class SeriesPayload:
    payload: bytes


SeriesMessage: TypeAlias = SeriesStart | SeriesEnd | SeriesPayload


async def dummy_sender(log: structlog.BoundLogger) -> AsyncIterator[SeriesMessage]:
    while True:
        await asyncio.sleep(1)
        log.info("sender: series start")
        yield SeriesStart(series_id=1337)
        await asyncio.sleep(2)
        log.info("sender: payload")
        yield SeriesPayload(b"hello world")
        await asyncio.sleep(2)
        log.info("sender: series end")
        yield SeriesEnd()


async def udp_receiver(
    log: structlog.BoundLogger, sock: asyncudp.Socket
) -> AsyncIterator[UdpRequest]:
    while True:
        data, addr = await sock.recvfrom()
        log.info(f"received {data} from {addr}")
        assert isinstance(data, bytes)
        decoded = decode_udp_request(data, addr)
        if decoded is not None:
            yield decoded


T = TypeVar("T")
U = TypeVar("U")


async def _await_next(iterator: AsyncIterator[T]) -> T:
    return await iterator.__anext__()


def _as_task(iterator: AsyncIterator[T]) -> asyncio.Task[T]:
    return asyncio.create_task(_await_next(iterator))


async def merge_iterators(
    a: AsyncIterator[T], b: AsyncIterator[U]
) -> AsyncIterable[T | U]:
    atask = _as_task(a)
    btask = _as_task(b)
    while True:
        done, _ = await asyncio.wait(
            (atask, btask), return_when=asyncio.FIRST_COMPLETED
        )
        if atask in done:
            yield atask.result()
            atask = _as_task(a)
        else:
            yield btask.result()
            btask = _as_task(b)


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    sender = dummy_sender(parent_log.bind(system="sender"))
    sock = await asyncudp.create_socket(local_addr=("localhost", args.udp_port))
    receiver = udp_receiver(log=parent_log.bind(system="udp"), sock=sock)

    series_id = 1
    bytes_in_frame = {0: 69321}
    with Path("/home/pmidden/Downloads/11064.PDF").open("rb") as input_file:
        file_contents = input_file.read()

        async for msg in merge_iterators(sender, receiver):
            match msg:
                case UdpPing(addr):
                    parent_log.info("received ping, sending pong")
                    sock.sendto(
                        encode_udp_reply(
                            UdpPong((series_id, len(bytes_in_frame.keys())))
                        ),
                        addr,
                    )
                case UdpPacketRequest(addr, frame_number, start_byte):
                    PACKET_SIZE = 1000
                    parent_log.info(
                        f"received packet request, frame {frame_number}, sending {file_contents[start_byte:start_byte+PACKET_SIZE]}"
                    )
                    sock.sendto(
                        encode_udp_reply(
                            UdpPacketReply(
                                frame_number=frame_number,
                                start_byte=start_byte,
                                bytes_in_frame=bytes_in_frame[frame_number],
                                payload=file_contents[
                                    start_byte : start_byte + PACKET_SIZE
                                ],
                            ),
                        ),
                        addr,
                    )
                case _:
                    parent_log.info(f"received something else: {msg}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
