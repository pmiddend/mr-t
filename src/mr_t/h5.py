import culsans
import structlog

from mr_t.eiger_stream1 import ZmqMessage

parent_log = structlog.get_logger()


def h5_writer_process(queue: culsans.SyncQueue[int]) -> None:
    parent_log.info("in writer process, starting main loop")
    while True:
        element = queue.get()  # noqa: F841
        parent_log.info(f"got new element, queue size now: {queue.qsize()}")
        parent_log.info("sleeping a bit")
        # time.sleep(5)
    queue.join()


async def write_to_h5(msg: ZmqMessage, q: culsans.AsyncQueue[ZmqMessage]) -> None:
    parent_log.info("writing to h5")
    await q.put(msg)
    parent_log.info("writing to h5 DONE")
