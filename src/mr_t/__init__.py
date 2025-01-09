import asyncio
import json
import multiprocessing
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, TypeAlias

import asyncudp
import culsans
import structlog
import zmq
import zmq.asyncio
from tap import Tap

parent_log = structlog.get_logger()


class Arguments(Tap):
    detector_zmq_host: str
    detector_zmq_port: int = 9999
    udp_server: str
    udp_port: int


def get_zmq_header(msg: list[zmq.Frame]) -> dict[str, Any]:
    return json.loads(msg[0].bytes.decode())


ZmqAppendix: TypeAlias = str | dict[str, Any]


@dataclass(frozen=True)
class ZmqHeader:
    series_id: str
    config: None | dict[str, Any]
    appendix: None | ZmqAppendix


@dataclass(frozen=True)
class ZmqSeriesEnd:
    pass


@dataclass(frozen=True)
class ZmqImage:
    pass


ZmqMessage: TypeAlias = ZmqHeader | ZmqSeriesEnd | ZmqImage


def decode_zmq_appendix(appendix: bytes) -> ZmqAppendix:
    try:
        appendix_str = appendix.decode()
    except UnicodeDecodeError:
        # appendix is not a string and probably cannot serialized to json
        return "UNICODE_DECODE_ERROR"
    try:
        return json.loads(appendix_str)
    except json.JSONDecodeError:
        # appendix is not a json string, send it as is
        return appendix_str


def decode_zmq_message(parts: list[zmq.Frame]) -> ZmqMessage:
    header = get_zmq_header(parts)
    htype = header["htype"]

    if htype == "dimage-1.0":
        meta = json.loads(parts[1].bytes.decode())
        shape = tuple(meta["shape"][::-1])  # Eiger shape order is reversed
        dtype = meta["type"]
        size = meta["size"]
        encoding = meta["encoding"]
        data = parts[2].buffer  # get a memoryview instead of a bytes copy
        config = json.loads(parts[3].bytes.decode())

        if len(parts) == 5:
            appendix = parts[4].bytes
        elif len(parts) == 4:
            appendix = None
        else:
            raise ValueError(
                "Unexpected number of parts in image message: %s", len(parts)
            )

        return ZmqImage()

    if htype == "dheader-1.0":
        detail = header["header_detail"]
        n_parts = len(parts)
        has_appendix = False
        if detail == "none":
            if n_parts == 1:
                has_appendix = False
            elif n_parts == 2:
                has_appendix = True
            else:
                raise ValueError("Unexpected number of parts: {}".format(n_parts))
        elif detail == "basic":
            if n_parts == 2:
                has_appendix = False
            elif n_parts == 3:
                has_appendix = True
            else:
                raise ValueError("Unexpected number of parts: {}".format(n_parts))
        elif detail == "all":
            if n_parts == 8:
                has_appendix = False
            elif n_parts == 9:
                has_appendix = True
            else:
                raise ValueError("Unexpected number of parts: {}".format(n_parts))

        config = (
            json.loads(parts[1].bytes.decode()) if detail in ["basic", "all"] else None
        )

        appendix = decode_zmq_appendix(parts[-1].bytes) if has_appendix else None

        return ZmqHeader(appendix=appendix, config=config, series_id=header["series"])

    if htype == "dseries_end-1.0":
        return ZmqSeriesEnd()
    raise ValueError("Unsupported htype: '{}'".format(htype))


async def receive_zmq_messages(
    zmq_target: str, log: structlog.BoundLogger
) -> AsyncIterator[ZmqMessage]:
    zmq_context = zmq.asyncio.Context()  # type: ignore

    zmq_socket = zmq_context.socket(zmq.PULL)
    zmq_socket.connect(zmq_target)

    log.info("connect called; might not be connected yet, waiting for first message")

    while True:
        try:
            msg = await zmq_socket.recv_multipart(copy=False)
            log.info(f"received zmq msg, decoding")
            yield decode_zmq_message(msg)
        except zmq.ContextTerminated:
            log.error("ZMQ context was terminated")


def h5_writer_process(queue: culsans.SyncQueue[int]) -> None:
    parent_log.info("in writer process, starting main loop")
    while True:
        element = queue.get()
        parent_log.info(f"got new element, queue size now: {queue.qsize()}")
        parent_log.info("sleeping a bit")
        time.sleep(5)
    queue.join()


async def write_to_h5(msg: ZmqMessage, q: culsans.AsyncQueue[ZmqMessage]) -> None:
    parent_log.info("writing to h5")
    await q.put(msg)
    parent_log.info("writing to h5 DONE")


async def write_to_udp(msg, udp_socket) -> None:
    parent_log.info("writing to udp")


class UdpTransmissionCoordinator:
    @staticmethod
    async def from_args(args: Arguments) -> "UdpTransmissionCoordinator":
        udp_socket = await asyncudp.create_socket(
            remote_addr=(args.udp_server, args.udp_port)
        )
        return UdpTransmissionCoordinator(udp_socket)

    def __init__(self, socket: asyncudp.Socket) -> None:
        self._socket = socket
        self._message_id = 1

    async def process_message(self, msg: ZmqMessage) -> None:
        if not isinstance(msg, ZmqImage):
            return

        self._socket.sendto(f"{self._message_id}foo".encode(encoding="utf-8"))
        self._message_id += 1


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    h5_queue = culsans.Queue(5)
    h5_writer_future = asyncio.get_running_loop().run_in_executor(
        None, h5_writer_process, h5_queue.sync_q
    )

    udp = await UdpTransmissionCoordinator.from_args(args)

    zmq_target = f"tcp://{args.detector_zmq_host}:{args.detector_zmq_port}"
    async for msg in receive_zmq_messages(
        zmq_target,
        parent_log.bind(zmq_target=zmq_target),
    ):
        parent_log.info(f"decoded zmq message: {msg}")

        # Exceptions are propagated here, so any failure leads to
        # immediate cancellation of the whole process. In the end, we
        # might want to be more error tolerant, but for now, leave it.
        await asyncio.gather(
            write_to_h5(msg, h5_queue.async_q), udp.process_message(msg)
        )

    await h5_writer_future


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
