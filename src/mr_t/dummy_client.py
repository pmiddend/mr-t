from dataclasses import dataclass, replace
import struct
import asyncio
import asyncudp
from tap import Tap
import structlog
from typing import TypeAlias, cast

parent_log = structlog.get_logger()


class Arguments(Tap):
    udp_server: str
    udp_port: int


@dataclass(frozen=True)
class UdpStartSeries:
    series_id: int
    number_of_packets: int


@dataclass(frozen=True)
class UdpImage:
    packet_id: int
    data: bytes


@dataclass(frozen=True)
class SeriesState:
    series_id: int
    number_of_packets: int
    current_packet: int


UdpMessage: TypeAlias = UdpStartSeries | UdpImage


def parse_message(data: bytes) -> None | UdpMessage:
    if not data:
        return None
    message_type = data[0]
    if message_type == 0:
        try:
            result = struct.unpack("<ii", data[1:])
            return UdpStartSeries(series_id=result[0], number_of_packets=int(result[1]))
        except:
            parent_log.warning(
                f"got a start series package, but followed by: {data[1:]}"
            )
            return None
    if message_type == 1:
        try:
            result = struct.unpack("<i", data[1:])
        except:
            parent_log.warning(f"got a packet package, but followed by: {data[1:5]}")
            return None
        return UdpImage(packet_id=result[0], data=data[5:])
    parent_log.warning(f"got an invalid package starting with byte {data[0]}")


async def main_async() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

    sock = await asyncudp.create_socket(remote_addr=(args.udp_server, args.udp_port))

    msg_no = 0
    current_series: None | SeriesState = None
    while True:
        parent_log.info("waiting for package")
        if current_series is None:
            sock.sendto(b"start_series")
        else:
            sock.sendto(struct.pack("<i", current_series.current_packet))
        data, _ = await sock.recvfrom()
        decoded = parse_message(cast(bytes, data))
        if decoded is None:
            continue
        if isinstance(decoded, UdpStartSeries):
            if current_series is None or current_series.series_id != decoded.series_id:
                parent_log.info(
                    f"new series {decoded.series_id} has started, {decoded.number_of_packets} packets"
                )
                current_series = SeriesState(
                    series_id=decoded.series_id,
                    number_of_packets=decoded.number_of_packets,
                    current_packet=0,
                )
            else:
                parent_log.info(
                    f"unnecessary new series {decoded.series_id} has started, ignoring"
                )
        else:
            if current_series is None:
                parent_log.warning("received packet, but have no series yet")
                continue
            if decoded.packet_id < current_series.current_packet:
                parent_log.info(f"old packet {decoded.packet_id} received, ignoring")
            elif decoded.packet_id > current_series.current_packet:
                parent_log.warning(
                    f"too new packet {decoded.packet_id} received, ignoring"
                )
            else:
                if (
                    current_series.current_packet + 1
                    >= current_series.number_of_packets
                ):
                    parent_log.info("finished series")
                else:
                    parent_log.info(
                        f"current package id increased to {current_series.current_packet + 1}"
                    )
                    current_series = replace(
                        current_series, packet_id=current_series.current_packet + 1
                    )

        msg_no += 1


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
