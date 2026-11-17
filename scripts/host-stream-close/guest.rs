use std::io::{Read, Write};
use std::net::TcpStream;
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};

unsafe extern "C" {
    fn setsockopt(fd: i32, level: i32, option: i32, value: *const u8, length: u32) -> i32;
}

fn option(stream: &TcpStream, name: i32, value: &[i32]) {
    assert_eq!(
        unsafe {
            setsockopt(
                stream.as_raw_fd(),
                1,
                name,
                value.as_ptr().cast(),
                std::mem::size_of_val(value) as u32,
            )
        },
        0
    );
}

fn main() {
    let config = std::fs::read_to_string("/close-config")
        .or_else(|_| std::fs::read_to_string("close-config"))
        .unwrap();
    let fields: Vec<_> = config.split_whitespace().collect();
    let ip = if fields[0] == "v6" {
        "::1"
    } else {
        "127.0.0.1"
    };
    let mut stream = TcpStream::connect((ip, fields[1].parse::<u16>().unwrap())).unwrap();
    let mut control = TcpStream::connect((ip, fields[2].parse::<u16>().unwrap())).unwrap();
    control
        .set_read_timeout(Some(Duration::from_secs(20)))
        .unwrap();
    let mode = fields[3];
    let slow = matches!(mode, "graceful" | "timeout" | "concurrent");
    let enabled = i32::from(matches!(mode, "abort" | "fast" | "timeout" | "concurrent"));
    let seconds = if mode == "abort" {
        0
    } else if mode == "fast" {
        2
    } else {
        1
    };
    option(&stream, 13, &[enabled, seconds]); // Linux SO_LINGER.
    option(&stream, 7, &[8192]); // Linux SO_SNDBUF.
    stream.write_all(b"X").unwrap();
    let mut ack = [0];
    stream.read_exact(&mut ack).unwrap();
    assert_eq!(ack, *b"A");
    stream.set_nonblocking(true).unwrap();
    let mut accepted = 0usize;
    if slow {
        let payload = [b'Z'; 65536];
        let until = Instant::now() + Duration::from_secs(2);
        while Instant::now() < until {
            match stream.write(&payload) {
                Ok(n) => {
                    assert!(n > 0);
                    accepted += n;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(e) => panic!("send: {e}"),
            }
            assert!(accepted < 64 * 1024 * 1024, "peer never backpressured");
        }
    }
    let mut echo_us = 0;
    let mut waiting = false;
    let close_us = if mode == "concurrent" {
        let (started, ready) = std::sync::mpsc::sync_channel(0);
        let closer = std::thread::spawn(move || {
            let before = Instant::now();
            started.send(()).unwrap();
            drop(stream);
            before.elapsed().as_micros()
        });
        ready.recv().unwrap();
        std::thread::sleep(Duration::from_millis(50));
        let before = Instant::now();
        control.write_all(b"P").unwrap();
        control.read_exact(&mut ack).unwrap();
        assert_eq!(ack, *b"P");
        echo_us = before.elapsed().as_micros();
        waiting = !closer.is_finished();
        closer.join().unwrap()
    } else {
        let before = Instant::now();
        drop(stream);
        before.elapsed().as_micros()
    };
    // Keep the VM alive until the peer has independently observed EOF/reset
    // and counted every byte; VM teardown must not masquerade as socket close.
    control.write_all(b"D").unwrap();
    control.read_exact(&mut ack).unwrap();
    assert_eq!(ack, *b"D");
    println!(
        "CLOSE_RESULT accepted={accepted} close_us={close_us} echo_us={echo_us} waiting={}",
        u8::from(waiting)
    );
}
