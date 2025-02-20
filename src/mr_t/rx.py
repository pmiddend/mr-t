from reactivex.abc import DisposableBase, ObserverBase, SchedulerBase
import zmq
from reactivex import Observable, create


def zmq_client(url: str) -> Observable[list[zmq.Frame]]:
    context = zmq.Context()

    zmq_socket = context.socket(zmq.PULL)
    zmq_socket.connect(url)

    def connect_callback(
        observer: ObserverBase[list[zmq.Frame]], scheduler: None | SchedulerBase
    ) -> DisposableBase:
        try:
            msg = zmq_socket.recv_multipart(copy=False)
            observer.on_next(msg)
        except Exception as e:
            observer.on_error(e)

    return create(connect_callback)
