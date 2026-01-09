# mr-t - connect to Eiger, stream to file and UDP

[![CI](https://github.com/pmiddend/mr-t/actions/workflows/ci.yaml/badge.svg)](https://github.com/pmiddend/mr-t/actions/workflows/ci.yaml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

## Python setup

This project uses [uv](https://docs.astral.sh/uv/) to manage its dependencies. If you're using Nix, there's also a `flake.nix` to get you started (`nix develop .#uv2nix` works to give you a dev environment).

Code formatting and checking is done with [ruff](https://docs.astral.sh/ruff/), just use `ruff format src`.

## Running mr-t — attached to a Simplon stream

If you have uv installed (see above) running the main program should be as easy as:

```
uv run mr_t_server --eiger-zmq-host-and-port $host --udp-host localhost --udp-port 9000
```

Which will receive images from the Dectris detector `$host:9999` and also listen for UDP messages on `localhost:9000`. Instead of using an actual Detector, you can also use one of the [Simplon](https://github.com/pmiddend/simplon-stub) API [mocks](https://github.com/AustralianSynchrotron/ansto-simplon-api).

You can also just use plain Python, of course:

```
python src/mr_t/server.py --eiger-zmq-host-and-port $host --udp-host localhost --udp-port 9000
```

Note that you have to install the dependencies mentioned in `pyproject.toml` beforehand (to a `venv`, for example).

There is a configurable `--frame-cache-limit` which, if you set it, will limit the number of frames held in memory to be no higher than this number. Meaning, the ZeroMQ messages will be held until the receiver picks them up.

## Running mr-t — feed from an HDF5 file

If you already have a finished image series stored in an HDF5 file, you can tell mr-t to read images from this file, instead of waiting for images via ZMQ. A sample command line looks like this:

```
uv run mr_t_server --input-h5-file $myhdf5file --frame-cache-limit 5 --udp-host localhost --udp-port 9000
```

Note that in addition to `--input-h5-file` we are passing `--frame-cache-limit 5`. This will read at most 5 frames from the HDF5 file and wait until the other side (the FPGA) has actually pulled images from this cache. If you don't do this, and the receiving end is too slow, you will eat up a lot of RAM with all the cached images.

Currently only one HDF5 file is supported to be read, since the Dectris detectors write a `master` file which you can specify if you want to feed a whole image series.

## How it works

### Main loop

In [server.py](https://github.com/pmiddend/mr-t/blob/main/src/mr_t/server.py), Mr. T will open a UDP socket as a server (a listen socket) on startup. It will also open a ZeroMQ socket to the detector and listen for messages itself. In Python, both the listening on ZeroMQ and on UDP are implemented as `async` functions returning `AsyncIterator[UdpRequest]` and `AsyncIterator[ZmqMessage]`, respectively, so that you can iterate over them via:

```python
async for msg in iterator:
   ...
```

The `merge_iterators` function will merge both iterators and return a union of both data types, so you can listen for both types of messages.

This message loops keeps two pieces of state:

1. The *current image series* (`CurrentSeries` in the code), which is optional, since there might no be a current series. It consists of the from the detector, the number of frames in it, the `saved_frames` (which is a dictionary from frame number to raw image data), the last complete frame (see below) and whether it officially ended.
2. The *last series ID*, which is used as a counter (new series always get the last ID + 1)

### UDP messages

Given a *UDP ping*, we simply return a *pong* with the current series information if we have it, or `None`.

Given a *UDP packet request* for a frame `frameno`, there are a few considerations:

- If we are not in a series at all, we ignore the packet request completely.
- If the frame requested is not in the current series' `saved_frames` cache, send a reply with no bytes in it. The client is supposed to ignore this.
- If we get a packet request for an unknown frame (like in the bullet point above) and the current series has ended, then send a reply with `premature_end_frame` set to the last complete frame, indicating to the client to stop requesting new frames.
- Otherwise, we have data to send to the UDP client. Send a reply with the requested frame slice.
- Afterwards, check if we have frames in `saved_frames` that are smaller than the requested frame and delete them.

### ZeroMQ messages

A *ZmqHeader* message has to contain a `config` dictionary, which should be there if the `header_detail` for the stream subsystem of the Dectris detector is set to `all` or `basic` (the default). We need the config because it gives us the `nimages` and `ntrigger` values, determining how many frames we have in each series. We then fill the `CurrentSeries` structure with a new series ID (monotonically increasing the last one, starting at 0) and mostly zero values. We also store the new series ID value in `last_series_id`, so that the counter can increase next time.

If the *ZmqHeader* contains a "header appendix" — which you can set via a Simplon API call — then this will be taken, verbatim, as the "series name". This is so the experimenter can name the current dataset with a human-readable name and not just a numeric ID. If the appendix is not given, this series name will be `series$id` with the detector-provided series ID.

A *ZmqImage* message contains a `memoryview` with the whole frame's data (there is a per-image `config` that you can set, too, but we don't use it). The frame's ID we generate ourselves by taking the last frame's number in the `saved_frames` dictionary and increasing by 1 (or taking 0 if we don't have any frames yet). We also remember the ID as the last complete frame (which is used for premature end of series). The message also contains the image's bit depth, size and encoding (for example, if it was compressed). We want to transport that information to the receiver, so we wait for the first frame and store the frame's information for later sending.

A *ZmqSeriesEnd* simply sets the current series' `ended` boolean to `True`.

See the [latest Dectris Simplon API](https://media.dectris.com/filer_public/6d/57/6d5779b4-2c8c-45a7-8792-6ef447f1ddde/simplon_apireference_v1p8.pdf).

## UDP protocol

Every UDP message starts with the _message type_ (one byte) and then there's the payload which depends on the message being sent. Generally, numbers are sent in _big endian_.

There are _four_ types of UDP messages that are sent back and forth between the client (the system requesting images) and the server (Mr. T):

- **Ping** (message type 0): has no content (so it's just 1 byte long), is sent from the client to the server. Will be answered by a Pong (see below)
- **Pong** (message type 1)
  1. _series ID_ (32 bit unsigned integer) of the image series currently going on, or 0 if there is no image series
  2. _bit depth_ (8 bit unsigned integer) of the images in the series
  3. _image width_ (16 bit unsigned integer) of the images in the series
  4. _image height_ (16 bit unsigned integer) of the images in the series
  5. _frame count_ (32 bit unsigned integer) of the current series (or 0 if there is no image series)
  6. _length of series name_ (16 bit unsigned integer)
  7. _series name_ (raw bytes, latin1 encoded, not zero terminated)
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
