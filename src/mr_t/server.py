import asyncio
from dataclasses import dataclass
import struct
from typing import Any, AsyncIterable, AsyncIterator, TypeAlias, TypeVar

import asyncudp
import structlog
from tap import Tap

from mr_t.eiger_zmq import ZmqHeader, ZmqImage, ZmqSeriesEnd, receive_zmq_messages

parent_log = structlog.get_logger()


class Arguments(Tap):
    udp_port: int
    eiger_zmq_host_and_port: str


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
    payload: memoryview


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


@dataclass
class CurrentSeries:
    series_id: int
    frame_count: int
    saved_frames: dict[int, memoryview]
    ended: bool


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    sender = receive_zmq_messages(
        zmq_target=args.eiger_zmq_host_and_port, log=parent_log.bind(system="sender")
    )
    sock = await asyncudp.create_socket(local_addr=("localhost", args.udp_port))
    receiver = udp_receiver(log=parent_log.bind(system="udp"), sock=sock)

    last_series_id = 0
    current_series: CurrentSeries | None = None
    async for msg in merge_iterators(sender, receiver):
        match msg:
            case UdpPing(addr):
                parent_log.info("received ping, sending pong")
                sock.sendto(
                    encode_udp_reply(
                        UdpPong((current_series.series_id, current_series.frame_count))
                        if current_series is not None and current_series.saved_frames
                        else UdpPong(None)
                    ),
                    addr,
                )
            case UdpPacketRequest(addr, frame_number, start_byte):
                if current_series is None:
                    continue
                this_frame = current_series.saved_frames.get(frame_number)

                # We might not have received this frame yet (client is faster than server)
                if this_frame is None:
                    continue

                PACKET_SIZE = 10000
                parent_log.info(f"received packet request, frame {frame_number}")

                sock.sendto(
                    encode_udp_reply(
                        UdpPacketReply(
                            frame_number=frame_number,
                            start_byte=start_byte,
                            bytes_in_frame=len(this_frame),
                            payload=this_frame[start_byte : start_byte + PACKET_SIZE],
                        ),
                    ),
                    addr,
                )

                saved_frame_ids = list(current_series.saved_frames)
                for fid in saved_frame_ids:
                    if fid < frame_number:
                        parent_log.info(f"deleting old frame {fid}")
                        current_series.saved_frames.pop(fid)
            case ZmqHeader(appendix, config, series_id):
                assert config is not None
                nimages = config.get("nimages")
                ntrigger = config.get("ntrigger")
                assert nimages is not None and ntrigger is not None
                assert isinstance(nimages, int) and isinstance(ntrigger, int)
                current_series = CurrentSeries(
                    series_id=last_series_id + 1,
                    frame_count=max(nimages, ntrigger),
                    saved_frames={},
                    ended=False,
                )
                last_series_id = current_series.series_id
                parent_log.info(f"series start, new ID {current_series.series_id}")
            case ZmqImage(data):
                assert current_series is not None
                new_frame_id = (
                    max(current_series.saved_frames) + 1
                    if current_series.saved_frames
                    else 0
                )
                current_series.saved_frames[new_frame_id] = data
                parent_log.info(f"image {new_frame_id} received")
            case ZmqSeriesEnd():
                parent_log.info(f"series ended")
                assert current_series is not None
                current_series.ended = True


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
