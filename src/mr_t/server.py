import asyncio
from dataclasses import dataclass
import struct
import sys
import logging
from typing import Any, AsyncIterable, AsyncIterator, TypeAlias, TypeVar

import asyncudp
import structlog
from tap import Tap

from mr_t.eiger_stream1 import ZmqHeader, ZmqImage, ZmqSeriesEnd, receive_zmq_messages

parent_log = structlog.get_logger()


class Arguments(Tap):
    udp_port: int
    udp_host: str
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
    premature_end_frame: int
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
        case UdpPacketReply(
            premature_end_frame, frame_number, start_byte, bytes_in_frame, payload
        ):
            return (
                struct.pack(
                    ">BIIII",
                    3,
                    premature_end_frame,
                    frame_number,
                    start_byte,
                    bytes_in_frame,
                )
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
        # This is way too much information, logging the raw output
        # log.info(f"received {data} from {addr}")
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


FrameNumber = int


@dataclass
class CurrentSeries:
    # This is *not* the series ID from the detector, but rather our
    # own, which is strictly monotonically increasing.
    series_id: int
    # How many frames in the current series
    frame_count: int
    saved_frames: dict[FrameNumber, memoryview]
    # Important for the case of a premature abort. For example, say
    # the detector delivered frame 10 (completely), and was aborted
    # afterwards. The client connecting to Mr. T would get frame 10,
    # and then start retrieving frame 11. This doesn't exist, of
    # course, but the client needs a way to distinguish that case from
    # "doesn't exist and will never exist because we aborted the
    # series".
    last_complete_frame: int
    ended: bool


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    sender = receive_zmq_messages(
        zmq_target=args.eiger_zmq_host_and_port, log=parent_log.bind(system="eiger")
    )
    sock = await asyncudp.create_socket(local_addr=(args.udp_host, args.udp_port))
    receiver = udp_receiver(log=parent_log.bind(system="udp"), sock=sock)

    last_series_id = 0
    current_series: CurrentSeries | None = None
    async for msg in merge_iterators(sender, receiver):
        match msg:
            case UdpPing(addr):
                parent_log.debug("received ping, sending pong")
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
                    parent_log.warning(
                        f"request for frame number {frame_number} ignored, not in series"
                    )
                    continue
                this_frame = current_series.saved_frames.get(frame_number)

                # We might not have received this frame yet (client is
                # faster than server), or we will never receive it due
                # to an abort
                if this_frame is None and current_series.ended:
                    sock.sendto(
                        encode_udp_reply(
                            UdpPacketReply(
                                premature_end_frame=current_series.last_complete_frame,
                                frame_number=frame_number,
                                start_byte=start_byte,
                                bytes_in_frame=0,
                                payload=memoryview(bytearray()),
                            ),
                        ),
                        addr,
                    )
                    continue

                # This is the case of a client that's too fast for the server
                if this_frame is None:
                    sock.sendto(
                        encode_udp_reply(
                            UdpPacketReply(
                                premature_end_frame=0,
                                frame_number=frame_number,
                                start_byte=start_byte,
                                # This signifies the frame isn't there yet
                                bytes_in_frame=0,
                                payload=memoryview(bytearray()),
                            ),
                        ),
                        addr,
                    )
                    continue

                PACKET_SIZE = 10000
                parent_log.debug(f"received packet request, frame {frame_number}")

                if start_byte > len(this_frame):
                    parent_log.warning(
                        f"start byte {start_byte} is after last byte in frame: {len(this_frame)}"
                    )

                sock.sendto(
                    encode_udp_reply(
                        UdpPacketReply(
                            premature_end_frame=0,
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
            case ZmqHeader(series_id, config, appendix):
                if config is None:
                    raise Exception(
                        'Got a ZMQ header message, but have no config values; have you configured "header_detail" to be "none"? It has to be either "basic" or "all" (preferably "basic").'
                    )
                nimages = config.get("nimages")
                ntrigger = config.get("ntrigger")
                assert nimages is not None and ntrigger is not None
                assert isinstance(nimages, int) and isinstance(ntrigger, int)
                current_series = CurrentSeries(
                    series_id=last_series_id + 1,
                    frame_count=nimages * ntrigger,
                    saved_frames={},
                    ended=False,
                    last_complete_frame=0,
                )
                last_series_id = current_series.series_id
                parent_log.info(f"series start, new ID {current_series.series_id}")
            case ZmqImage(data):
                if current_series is None:
                    parent_log.warning(
                        "got a ZmqImage message but we have no series, what the hell went wrong here?"
                    )
                assert current_series is not None
                new_frame_id = (
                    max(current_series.saved_frames) + 1
                    if current_series.saved_frames
                    else 0
                )
                current_series.last_complete_frame = new_frame_id
                current_series.saved_frames[new_frame_id] = data
                parent_log.info(f"image {new_frame_id} received")
            case ZmqSeriesEnd():
                if current_series is None:
                    parent_log.warning(
                        "got a ZmqSeriesEnd message but we have no series, what the hell went wrong here?"
                    )
                parent_log.info("series ended")
                assert current_series is not None
                current_series.ended = True
        if current_series is not None:
            sys.stderr.write(
                f"\r series ID {current_series.series_id: >4} cached frames: {len(current_series.saved_frames.keys())}"
            )
        else:
            sys.stderr.write("\rno series")


def main() -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO)
    )
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
