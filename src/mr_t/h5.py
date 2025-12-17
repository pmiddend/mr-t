import asyncio
from pathlib import Path
from typing import AsyncIterator
from typing import Callable

import h5py
import structlog

from mr_t.eiger_stream1 import ZmqHeader
from mr_t.eiger_stream1 import ZmqImage
from mr_t.eiger_stream1 import ZmqMessage
from mr_t.eiger_stream1 import ZmqSeriesEnd

parent_log = structlog.get_logger()


def _dataset_to_compression(ds: h5py.Dataset) -> str:
    pl = ds.id.get_create_plist()
    n_filters = pl.get_nfilters()
    assert isinstance(n_filters, int)
    if n_filters == 0:
        return ""
    has_lz4 = False
    has_bs = False
    for i in range(n_filters):
        filter_id, _flags, _cd_vals, _name = pl.get_filter(i)

        # https://github.com/DiamondLightSource/hdf5filters/blob/master/h5lzfilter/H5Zlz4.c#L22
        if filter_id == 32004:
            has_lz4 = True
        elif filter_id == 32008:
            has_bs = True
    return (
        "bs8-lz4<"
        if has_bs and has_lz4
        else "bs8<"
        if has_bs
        else "lz8<"
        if has_lz4
        else ""
    )


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
        for frame_number in range(frame_count):
            if cache_full():
                await asyncio.sleep(0.5)
                continue
            yield ZmqImage(
                dataset[frame_number][:].tobytes(),  # type: ignore
                # Should be something like "uint32"
                data_type=dataset.dtype.name,  # type: ignore
                compression=_dataset_to_compression(dataset),
            )
        yield ZmqSeriesEnd()
