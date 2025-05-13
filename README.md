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

## UDP protocol

Every UDP message starts with the _message type_ (one byte) and then there's the payload which depends on the message being sent. Generally, numbers are sent in _big endian_.

There are _four_ types of UDP messages that are sent back and forth between the client (the system requesting images) and the server (Mr. T):

- **Ping** (message type 0): has no content (so it's just 1 byte long), is sent from the client to the server. Will be answered by a Pong (see below)
- **Pong** (message type 1)
  1. _series ID_ (32 bit unsigned integer) of the image series currently going on, or 0 if there is no image series
  2. _frame count_ (32 bit unsigned integer) of the current series (or 0 if there is no image series)
- **Packet request** (message type 2)
  1. _frame number_ (32 bit unsigned integer, starting at zero) the frame number to get bytes from
  2. _start byte_ (32 bit unsigned integer, starting at zero) the start byte inside the requested frame
- **Packet reply** (message type 3)
  1. _premature end frame_ (32 bit unsigned integer): if this is 0, then the series is still going on; if it is not equal to zero, it's the index of the last frame in the series
  2. _frame number_ (32 bit unsigned integer): the frame number the payload is for 
  3. _start byte_ (32 bit unsigned integer): the starting byte inside the frame
  4. _bytes in frame_ (32 bit unsigned integer): how many bytes in total in this frame
  5. _payload_ (raw bytes): the actual bytes
  
The way the protocol works is as follows:

- The client keeps sending **Ping** to the server until it receives a **Pong** with a _series ID_ that is not zero.
- In this case, the client switches to requesting image data for the series. It sends **Packet request** messages, starting with _frame number_ 0, _start byte_ 0.
- It'll optionally (this is UDP, remember?) receive a **Packet reply** message with _frame number_ 0, _start byte_ 0, the total number of bytes in _bytes in frame_ and the some bytes inside the _payload_ (trying to fill up the UDP packet).
- The client will save this partial frame, and request _frame number_ 0, _start byte_ "n+1" (where n is the number of bytes in the first reply) next. Superfluous **Packet reply** (and **Pong**) messages have to be ignored by comparing the _start byte_ and _frame number_ with the latest one that was requested.
- The client can also switch to the next _frame number_ if the frame is already finished transferring, or possibly start sending **Ping** messages again, if the whole series is transferred.
- The client might receive **Packet reply** with _premature end of frame_ set to non-zero. This means the series is over before the previously communicated _frame count_ is reached (because we stopped the series prematurely). In that case, the client can ask for all data until the frame indicated by _premature end of frame_ and start sending **Ping** again.

# References

- All the ZMQ (v1) parsing code comes from Tim Schoof from the [asapo_eiger_connector](https://gitlab.desy.de/fs-sc/asapo_eiger_connector) repository.
- All the HDF5 writing code comes from Tim Schoof from the [asapo_eiger_connector](https://gitlab.desy.de/fs-sc/asapo_nexus_writer) repository.
