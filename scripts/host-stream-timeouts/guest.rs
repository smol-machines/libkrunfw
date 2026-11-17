use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd};
use std::time::{Duration, Instant};

unsafe extern "C" {
    fn socket(domain: i32, kind: i32, protocol: i32) -> i32;
    fn connect(fd: i32, address: *const u8, length: u32) -> i32;
    fn setsockopt(fd: i32, level: i32, option: i32, value: *const i32, length: u32) -> i32;
}

fn buffers(socket: &impl AsRawFd) {
    for option in [7, 8] {
        let capacity = 4096;
        assert_eq!(
            unsafe { setsockopt(socket.as_raw_fd(), 1, option, &capacity, 4) },
            0
        );
    }
}

fn timeouts(socket: &TcpStream) {
    socket
        .set_read_timeout(Some(Duration::from_millis(200)))
        .unwrap();
    socket
        .set_write_timeout(Some(Duration::from_millis(200)))
        .unwrap();
}

fn open(peer: SocketAddr, preconnect: bool) -> TcpStream {
    if !preconnect {
        return TcpStream::connect(peer).unwrap();
    }
    let fd = unsafe { socket(if peer.is_ipv4() { 2 } else { 10 }, 1, 0) };
    assert!(fd >= 0);
    let stream = unsafe { TcpStream::from_raw_fd(fd) };
    timeouts(&stream);
    let mut address = [0u8; 28];
    address[..2].copy_from_slice(&(if peer.is_ipv4() { 2u16 } else { 10u16 }).to_ne_bytes());
    address[2..4].copy_from_slice(&peer.port().to_be_bytes());
    match peer {
        SocketAddr::V4(a) => address[4..8].copy_from_slice(&a.ip().octets()),
        SocketAddr::V6(a) => address[8..24].copy_from_slice(&a.ip().octets()),
    }
    assert_eq!(
        unsafe { connect(fd, address.as_ptr(), if peer.is_ipv4() { 16 } else { 28 }) },
        0
    );
    stream
}

fn check(address: &str, phase: &str, write: bool) -> (u128, usize) {
    let listener = TcpListener::bind(address).unwrap();
    if phase == "inherited" {
        // Borrow the listener descriptor without transferring ownership.
        let listener_view =
            std::mem::ManuallyDrop::new(unsafe { TcpStream::from_raw_fd(listener.as_raw_fd()) });
        timeouts(&listener_view);
    }
    let client = open(listener.local_addr().unwrap(), phase == "preconnect");
    let (accepted, _) = listener.accept().unwrap();
    let (mut stream, other) = if phase == "accepted" || phase == "inherited" {
        (accepted, client)
    } else {
        (client, accepted)
    };
    if phase == "connected" || phase == "accepted" {
        timeouts(&stream);
    }
    assert_eq!(
        stream.read_timeout().unwrap(),
        Some(Duration::from_millis(200))
    );
    assert_eq!(
        stream.write_timeout().unwrap(),
        Some(Duration::from_millis(200))
    );
    buffers(&stream);
    buffers(&other);
    let mut transferred = 0;
    let data = vec![83; 1024 * 1024];
    loop {
        let started = Instant::now();
        let result = if write {
            stream.write(&data)
        } else {
            stream.read(&mut [0; 1])
        };
        let elapsed = started.elapsed();
        match result {
            Err(e) => {
                assert_eq!(e.kind(), std::io::ErrorKind::WouldBlock, "{e}");
                assert!(
                    elapsed >= Duration::from_millis(100) && elapsed < Duration::from_secs(3),
                    "deadline: {elapsed:?}"
                );
                assert!(!write || transferred > 0);
                return (elapsed.as_millis(), transferred);
            }
            Ok(n) => {
                assert!(write && n > 0, "read without a peer write: {n}");
                transferred += n;
                assert!(
                    transferred < 32 * 1024 * 1024,
                    "peer must exert backpressure"
                );
            }
        }
    }
}

fn main() {
    let filter = std::fs::read_to_string("timeout-filter").unwrap_or_default();
    for (label, address) in [("v4", "127.0.0.1:0"), ("v6", "[::1]:0")] {
        for phase in ["connected", "preconnect", "accepted", "inherited"] {
            for write in [false, true] {
                let name = format!("{label}-{phase}-{}", if write { "write" } else { "read" });
                if !filter.trim().is_empty() && filter.trim() != name {
                    continue;
                }
                let (elapsed, transferred) = check(address, phase, write);
                println!("TIMEOUT_CASE {name} PASS {elapsed} {transferred}");
            }
        }
    }
}
