import asyncio
from pathlib import Path
import h5py
from typing import AsyncIterator, Callable
import structlog

from mr_t.eiger_stream1 import ZmqHeader, ZmqImage, ZmqMessage, ZmqSeriesEnd

parent_log = structlog.get_logger()


async def receive_h5_messages(
    input_file: Path, log: structlog.BoundLogger, cache_full: Callable[[], bool]
) -> AsyncIterator[ZmqMessage]:
    log.info(f"begin reading frames from file {input_file}")

    with h5py.File(input_file, "r") as f:
        dataset = f["entry/data/data"]
        assert isinstance(dataset, h5py.Dataset)
        frame_count = len(dataset)
        yield ZmqHeader(
            series_id="1", config={"nimages": frame_count, "ntrigger": 1}, appendix=None
        )
        for frame_number in range(0, frame_count):
            if cache_full():
                await asyncio.sleep(0.5)
                continue
            yield ZmqImage(dataset[frame_number][:].tobytes())
        yield ZmqSeriesEnd()
