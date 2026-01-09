use std::fs::File;
use std::io::Cursor;
use std::io::Error;
use std::io::ErrorKind;
use std::io::Read;
use std::io::Write;
use std::net::UdpSocket;
use std::thread::sleep;
use std::time::Duration;

use byteorder::BigEndian;
use byteorder::ReadBytesExt;
use byteorder::WriteBytesExt;
use clap::Parser;
use log::{info, Level};
use stderrlog::Timestamp;

#[derive(Parser)]
struct Cli {
    udp_address: String,
}

enum LoopState {
    NoSeries {
        prior_series_id: Option<u32>,
    },
    InSeries {
        series_id: u32,
        frame_count: u32,
    },
    InFrame {
        series_id: u32,
        frame_count: u32,
        current_frame: u32,
        current_frame_total_bytes: u32,
        current_frame_byte: u32,
    },
}

struct PongPayload {
    series_id: u32,
    frame_count: u32,
    bits_per_pixel: u8,
    image_width: u16,
    image_height: u16,
    series_name: String
}

enum UdpRequest {
    UdpPing,
    UdpPacketRequest { frame_number: u32, start_byte: u32 },
}

enum UdpResponse {
    UdpPong {
        pong_payload: Option<PongPayload>,
    },
    UdpPacketReply {
        premature_end_frame: u32,
        frame_number: u32,
        start_byte: u32,
        bytes_in_frame: u32,
        payload: Vec<u8>,
    },
}

fn encode_request(msg: UdpRequest) -> Vec<u8> {
    match msg {
        UdpRequest::UdpPing => {
            let mut result = vec![];
            result.write_u8(0).unwrap();
            return result;
        }
        UdpRequest::UdpPacketRequest {
            frame_number,
            start_byte,
        } => {
            let mut result = vec![];
            result.write_u8(2).unwrap();
            result.write_u32::<BigEndian>(frame_number).unwrap();
            result.write_u32::<BigEndian>(start_byte).unwrap();
            return result;
        }
    }
}

fn decode_response(bytes: Vec<u8>) -> Result<UdpResponse, std::io::Error> {
    let mut reader = Cursor::new(bytes);

    let msg_type = reader.read_u8().unwrap();

    match msg_type {
        1 => {
            let series_id = reader.read_u32::<BigEndian>()?;

            if series_id == 0 {
                return Ok(UdpResponse::UdpPong {
                    pong_payload: Option::None,
                });
            }

	    let bits_per_pixel = reader.read_u8()?;

            let image_width = reader.read_u16::<BigEndian>()?;

            let image_height = reader.read_u16::<BigEndian>()?;

            let frame_count = reader.read_u32::<BigEndian>()?;

	    let _name_length = reader.read_u16::<BigEndian>()?;

            let mut series_name: Vec<u8> = vec![];
	    reader.read_to_end(&mut series_name)?;

            return Ok(UdpResponse::UdpPong {
                pong_payload: Option::Some(PongPayload {
                    series_id,
                    frame_count,
		    bits_per_pixel,
		    image_width,
		    image_height,
		    series_name: String::from_utf8_lossy(&series_name[..]).to_string(),
                }),
            });
        }
        3 => {
            let premature_end_frame = reader.read_u32::<BigEndian>()?;
            let frame_number = reader.read_u32::<BigEndian>()?;
            let start_byte = reader.read_u32::<BigEndian>()?;
            let bytes_in_frame = reader.read_u32::<BigEndian>()?;

            let mut payload: Vec<u8> = vec![];
            reader.read_to_end(&mut payload)?;
            return Ok(UdpResponse::UdpPacketReply {
                premature_end_frame,
                frame_number,
                start_byte,
                bytes_in_frame,
                payload,
            });
        }
        _ => return Err(Error::new(ErrorKind::Other, "invalid msg type")),
    }
}

fn udp_ping_pong(socket: &UdpSocket, msg: UdpRequest) -> Result<UdpResponse, std::io::Error> {
    match socket.send(&encode_request(msg)) {
        Err(e) => info!("send data failed: {e:?}"),
        Ok(_) => {}
    }

    let mut buf = vec![0; 65536];
    match socket.recv(&mut buf) {
        Ok(received) => {
            // println!("received {received} bytes {:?}", &buf[..received]);
            info!("received {received} bytes");
            if buf.len() == 0 {
                Err(Error::new(ErrorKind::Other, "received nothing"))
            } else {
                buf.resize(received, 0);
                decode_response(buf)
            }
        }
        Err(e) => {
            info!("recv data failed: {e:?}");
            Err(e)
        }
    }
}

fn main() {
    let args = Cli::parse();

    stderrlog::new()
        .module(module_path!())
        .verbosity(Level::Info)
        .timestamp(Timestamp::Millisecond)
        .init()
        .unwrap();

    let local_bind_addr = "localhost:0";

    let socket = UdpSocket::bind(local_bind_addr).expect(&format!(
        "couldn't bind UDP to {add}",
        add = local_bind_addr
    ));

    socket
        .set_read_timeout(Some(Duration::from_millis(200)))
        .expect("couldn't set read timeout for socket");

    socket.connect(&args.udp_address).expect(&format!(
        "couldn't connect to {add}",
        add = &args.udp_address
    ));
    info!("connected to {0}", args.udp_address);

    let mut state = LoopState::NoSeries {
        prior_series_id: Option::None,
    };
    let mut output_file = File::create("/tmp/output.txt").expect("cannot create file");
    loop {
        match state {
            LoopState::NoSeries { prior_series_id } => {
                // {
                //     let mut stdout = stdout().lock();
                //     let _ = stdout.write_all(b"hello\n");
                // }
                info!("no series, sending ping");
                match udp_ping_pong(&socket, UdpRequest::UdpPing) {
                    Ok(v) => match v {
                        UdpResponse::UdpPong { pong_payload } => match pong_payload {
                            Some(payload) => {
                                if Some(payload.series_id) == prior_series_id {
                                    info!("no series: same as last series, waiting");
                                    sleep(Duration::from_secs(2))
                                } else {
                                    info!("no series: new series (bit depth {0}, width {2}, height {3}, name {1}), switching state, waiting", payload.bits_per_pixel, payload.series_name, payload.image_width, payload.image_height);
                                    state = LoopState::InSeries {
                                        series_id: payload.series_id,
                                        frame_count: payload.frame_count,
                                    }
                                }
                            }
                            None => {
                                info!("no series: still no series");
                                sleep(Duration::from_secs(2));
                            }
                        },
                        UdpResponse::UdpPacketReply { .. } => {
                            info!("no series: got stray packet reply");
                        }
                    },
                    Err(_) => {
                        sleep(Duration::from_secs(2));
                    }
                }
            }
            LoopState::InSeries {
                series_id,
                frame_count,
            } => {
                info!("in series, sending initial request");
                match udp_ping_pong(
                    &socket,
                    UdpRequest::UdpPacketRequest {
                        frame_number: 0,
                        start_byte: 0,
                    },
                ) {
                    Ok(v) => match v {
                        UdpResponse::UdpPong { .. } => {
                            info!("in series: received stray pong");
                        }
                        UdpResponse::UdpPacketReply {
                            premature_end_frame,
                            frame_number,
                            start_byte: _,
                            bytes_in_frame,
                            payload,
                        } => {
                            if premature_end_frame > 0 {
                                info!("in series: premature end");
                                state = LoopState::NoSeries {
                                    prior_series_id: Some(series_id),
                                }
				
                            } else if bytes_in_frame == 0 {
				info!("in series: frame {frame_number} not there yet, waiting some more")
			    } else {
				
                                info!("in series: packet reply, switching to in frame");
                                output_file
                                    .write(&payload)
                                    .expect("cannot write to file for some reason");
                                state = LoopState::InFrame {
                                    series_id,
                                    frame_count,
                                    current_frame: frame_number,
                                    current_frame_total_bytes: bytes_in_frame,
                                    current_frame_byte: payload.len() as u32,
                                }
                            }
                        }
                    },
                    Err(_) => {}
                }
            }
            LoopState::InFrame {
                series_id,
                frame_count,
                current_frame,
                current_frame_total_bytes,
                current_frame_byte,
            } => {
                info!("in frame, received more bytes");
                let have_frame_switch = current_frame_byte >= current_frame_total_bytes;
                let new_current_frame = if have_frame_switch {
                    current_frame + 1
                } else {
                    current_frame
                };
                let new_start_byte = if have_frame_switch {
                    0
                } else {
                    current_frame_byte
                };

                if new_current_frame >= frame_count {
                    // info!("in frame: we're finished!");
                    // break;
                    state = LoopState::NoSeries {
                        prior_series_id: Some(series_id),
                    };
                    continue;
                }

                info!(
                    "in frame: requesting start byte {new_start_byte} of frame {new_current_frame}"
                );

                match udp_ping_pong(
                    &socket,
                    UdpRequest::UdpPacketRequest {
                        frame_number: new_current_frame,
                        start_byte: new_start_byte,
                    },
                ) {
                    Ok(v) => {
                        match v {
                            UdpResponse::UdpPong { .. } => {
                                info!("in frame: received stray pong");
                            }
                            UdpResponse::UdpPacketReply {
                                premature_end_frame,
                                frame_number,
                                start_byte,
                                bytes_in_frame,
                                payload,
                            } => {
                                if premature_end_frame > 0 {
                                    info!("in frame: premature end");
                                    state = LoopState::NoSeries {
                                        prior_series_id: Some(series_id),
                                    }
                                } else if bytes_in_frame == 0 {
				    info!("in frame: frame {frame_number} not there yet, waiting some more")
                                } else {
                                    if frame_number != new_current_frame {
                                        info!("in frame: got frame {frame_number}, expected {new_current_frame}")
                                    } else if start_byte != new_start_byte {
                                        info!("in frame: got start byte {start_byte}, expected {new_start_byte}")
                                    } else {
                                        let payload_len = payload.len();
                                        output_file
                                            .write(&payload)
                                            .expect("cannot write to file for some reason");
                                        if have_frame_switch {
                                            info!("in frame: frame switch received {payload_len} byte(s)");
                                            state = LoopState::InFrame {
                                                series_id,
                                                frame_count,
                                                current_frame: new_current_frame,
                                                current_frame_total_bytes: bytes_in_frame,
                                                current_frame_byte: payload_len as u32,
                                            }
                                        } else {
                                            info!("in frame: received {payload_len} byte(s)");
                                            state = LoopState::InFrame {
                                                series_id,
                                                frame_count,
                                                current_frame: new_current_frame,
                                                current_frame_total_bytes: bytes_in_frame,
                                                current_frame_byte: current_frame_byte
                                                    + payload_len as u32,
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Err(_) => {}
                }
            }
        }
    }
}
