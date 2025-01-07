# mr-t - connect to Eiger, stream to file and UDP

[![CI](https://github.com/pmiddend/mr-t/actions/workflows/ci.yaml/badge.svg)](https://github.com/pmiddend/mr-t/actions/workflows/ci.yaml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

## Python setup

This project uses [uv](https://docs.astral.sh/uv/) to manage its dependencies. If you're using Nix, there's also a `flake.nix` to get you started (`nix develop .#uv2nix` works to give you a dev environment).

Code formatting is done with [ruff](https://docs.astral.sh/ruff/), just use `ruff format src`.

## Running mr-t

If you have uv installed (see above) running the main program should be as easy as:

```
uv run mr_t --detector-zmq-host $host
```

Which will receive images from `$host:9999`.

# References

- All the ZMQ (v1) parsing code comes from Tim Schoof from the [asapo_eiger_connector](https://gitlab.desy.de/fs-sc/asapo_eiger_connector) repository.
