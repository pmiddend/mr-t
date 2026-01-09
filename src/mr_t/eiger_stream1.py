import asyncio
import json
from dataclasses import dataclass
from typing import Any
from typing import AsyncIterator
from typing import Callable

import structlog
import zmq
import zmq.asyncio


def get_zmq_header(msg: list[zmq.Frame]) -> dict[str, Any]:
    return json.loads(msg[0].bytes.decode())


type ZmqAppendix = str | dict[str, Any]


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
    data: memoryview
    shape: list[int]
    data_type: str
    compression: str


type ZmqMessage = ZmqHeader | ZmqSeriesEnd | ZmqImage


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
        # Eiger shape order is reversed
        shape = list(meta["shape"][::-1])
        # dtype = meta["type"]
        # size = meta["size"]
        # encoding = meta["encoding"]
        # get a memoryview instead of a bytes copy
        data = parts[2].buffer
        config = json.loads(parts[3].bytes.decode())

        if len(parts) == 5:
            appendix = parts[4].bytes
        elif len(parts) == 4:
            appendix = None
        else:
            raise ValueError(
                "Unexpected number of parts in image message: %s", len(parts)
            )

        return ZmqImage(data, shape, meta["type"], meta["encoding"])

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
                raise ValueError(
                    f'Unexpected number of parts for "none" detail: {n_parts}'
                )
        elif detail == "basic":
            if n_parts == 2:
                has_appendix = False
            elif n_parts == 3:
                has_appendix = True
            else:
                raise ValueError(
                    f'Unexpected number of parts for "basic" detail: {n_parts}'
                )
        elif detail == "all":
            if n_parts == 8:
                has_appendix = False
            elif n_parts == 9:
                has_appendix = True
            else:
                raise ValueError(
                    f'Unexpected number of parts for "all" detail: {n_parts}'
                )

        config = (
            json.loads(parts[1].bytes.decode()) if detail in ["basic", "all"] else None
        )

        appendix = decode_zmq_appendix(parts[-1].bytes) if has_appendix else None

        return ZmqHeader(appendix=appendix, config=config, series_id=header["series"])

    if htype == "dseries_end-1.0":
        return ZmqSeriesEnd()
    raise ValueError(f"Unsupported htype: '{htype}'")


async def receive_zmq_messages(
    zmq_target: str, log: structlog.BoundLogger, cache_full: Callable[[], bool]
) -> AsyncIterator[ZmqMessage]:
    # Somehow doesn't type-check, but it's a library issue
    zmq_context = zmq.asyncio.Context()  # type: ignore

    zmq_socket = zmq_context.socket(zmq.PULL)
    zmq_socket.connect(zmq_target)

    log.info(
        f"connect to '{zmq_target}' called; might not be connected yet, waiting for first message"
    )

    while True:
        if cache_full():
            await asyncio.sleep(0.5)
            continue
        try:
            msg = await zmq_socket.recv_multipart(copy=False)
            log.info("received zmq msg, decoding")
            yield decode_zmq_message(msg)
        except zmq.ContextTerminated:
            log.error("ZMQ context was terminated")
