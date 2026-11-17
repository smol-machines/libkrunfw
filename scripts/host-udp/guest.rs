//! Linux UDP conformance probe, runnable natively or inside a host-mode VM.
use std::{
    fs, io,
    net::{SocketAddr, UdpSocket},
    os::fd::AsRawFd,
    panic,
    time::Duration,
};

unsafe extern "C" {
    fn connect(fd: i32, address: *const u8, length: u32) -> i32;
}

fn disconnect(socket: &UdpSocket) -> io::Result<()> {
    let address = [0u8; 2]; // Linux AF_UNSPEC, including the minimal valid length.
    if unsafe { connect(socket.as_raw_fd(), address.as_ptr(), address.len() as u32) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

fn echo(socket: &UdpSocket, peer: SocketAddr, payload: &[u8]) {
    assert_eq!(socket.send(payload).unwrap(), payload.len());
    let mut bytes = [0; 128];
    let (length, source) = socket.recv_from(&mut bytes).unwrap();
    assert_eq!(&bytes[..length], payload);
    assert_eq!(source, peer);
}

fn case(fields: &[&str]) {
    let local: SocketAddr = fields[2].parse().unwrap();
    let peer: SocketAddr = fields[3].parse().unwrap();
    let alternate: SocketAddr = fields[4].parse().unwrap();
    let socket = UdpSocket::bind(local).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    socket.connect(peer).unwrap();
    let connected_local = socket.local_addr().unwrap();
    echo(&socket, peer, b"before");
    match fields[1] {
        "alternate" => {
            assert_eq!(socket.send_to(b"alternate", alternate).unwrap(), 9);
            let mut bytes = [0; 128];
            let (length, source) = socket.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..length], b"right-peer");
            assert_eq!(source, peer);
            socket
                .set_read_timeout(Some(Duration::from_millis(100)))
                .unwrap();
            let error = socket.recv_from(&mut bytes).unwrap_err();
            assert!(matches!(
                error.kind(),
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
            ));
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            assert_eq!(socket.peer_addr().unwrap(), peer);
            assert_eq!(socket.local_addr().unwrap(), connected_local);
            echo(&socket, peer, b"after-alternate");
        }
        "disconnect" => {
            disconnect(&socket).unwrap();
            disconnect(&socket).unwrap();
            assert_eq!(socket.peer_addr().unwrap_err().raw_os_error(), Some(107));
            // Linux releases an automatically allocated port, but retains a
            // user-selected port and an explicitly selected local address.
            assert_eq!(socket.local_addr().unwrap(), local);
            assert_eq!(
                socket.send(b"no-peer").unwrap_err().raw_os_error(),
                Some(89)
            );
            assert_eq!(socket.send_to(b"after-disconnect", alternate).unwrap(), 16);
            let mut bytes = [0; 128];
            let (length, source) = socket.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..length], b"after-disconnect");
            assert_eq!(source, alternate);
            socket.connect(peer).unwrap();
            echo(&socket, peer, b"reconnected");
        }
        "failed-family" => {
            let error = socket.connect("[::1]:53").unwrap_err();
            assert_eq!(error.raw_os_error(), Some(97));
            assert_eq!(socket.peer_addr().unwrap(), peer);
            assert_eq!(socket.local_addr().unwrap(), connected_local);
            echo(&socket, peer, b"after-failed-connect");
        }
        other => panic!("unknown test: {other}"),
    }
}

fn main() {
    let config = fs::read_to_string("udp-config").unwrap();
    let mut failed = 0;
    for line in config.lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        assert_eq!(fields.len(), 5);
        let passed = panic::catch_unwind(|| case(&fields)).is_ok();
        println!(
            "UDP_CASE {} {}",
            fields[0],
            if passed { "PASS" } else { "FAIL" }
        );
        failed += usize::from(!passed);
    }
    if failed != 0 {
        std::process::exit(1);
    }
    println!("UDP_CONFORMANCE_PASS");
}
