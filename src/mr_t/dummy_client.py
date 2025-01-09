import asyncio
import asyncudp
from tap import Tap
import structlog

parent_log = structlog.get_logger()


class Arguments(Tap):
    udp_server: str
    udp_port: int


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    sock = await asyncudp.create_socket(local_addr=(args.udp_server, args.udp_port))

    msg_no = 0
    while True:
        parent_log.info("waiting for package")
        data, addr = await sock.recvfrom()
        print(f"received {msg_no}")
        msg_no += 1


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
