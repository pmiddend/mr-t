from tap import Tap

class Arguments(Tap):
    detector_host: str
    detector_zmq_port: int = 9999

def main() -> None:
    args = Arguments(underscores_to_dashes=True).parse_args()

if __name__ == "__main__":
    main()
