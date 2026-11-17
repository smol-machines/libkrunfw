use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpListener, TcpStream, UdpSocket},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    time::Duration,
};
unsafe extern "C" {
    #[link_name = "socket"]
    fn raw_socket(domain: i32, kind: i32, protocol: i32) -> i32;
    fn bind(fd: i32, addr: *const u8, len: u32) -> i32;
    fn connect(fd: i32, addr: *const u8, len: u32) -> i32;
    fn getsockopt(fd: i32, level: i32, option: i32, value: *mut i32, length: *mut u32) -> i32;
    fn setsockopt(fd: i32, level: i32, option: i32, value: *const i32, length: u32) -> i32;
}
fn set(socket: &impl AsRawFd, level: i32, option: i32, value: i32) {
    assert_eq!(
        unsafe { setsockopt(socket.as_raw_fd(), level, option, &value, 4) },
        0,
        "setsockopt: {}",
        std::io::Error::last_os_error()
    );
}
fn shared_socket(ipv4: bool, port: u16, reuse_port: bool) -> UdpSocket {
    let fd = unsafe { raw_socket(if ipv4 { 2 } else { 10 }, 2, 0) };
    assert!(fd >= 0);
    let socket = unsafe { UdpSocket::from_raw_fd(fd) };
    set(&socket, 1, 2, 1);
    if reuse_port {
        set(&socket, 1, 15, 1);
    }
    let mut address = [0u8; 28];
    let family = if ipv4 { 2u16 } else { 10u16 };
    address[..2].copy_from_slice(&family.to_ne_bytes());
    address[2..4].copy_from_slice(&port.to_be_bytes());
    assert_eq!(
        unsafe { bind(fd, address.as_ptr(), if ipv4 { 16 } else { 28 }) },
        0,
        "{}",
        std::io::Error::last_os_error()
    );
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    socket
}

fn receive(socket: &UdpSocket, expected: &[u8]) {
    let mut data = [0; 64];
    let (n, _) = socket.recv_from(&mut data).unwrap();
    assert_eq!(&data[..n], expected);
}

fn get(socket: &impl AsRawFd, level: i32, option: i32) -> i32 {
    let mut value = -1;
    let mut length = 4;
    assert_eq!(
        unsafe { getsockopt(socket.as_raw_fd(), level, option, &mut value, &mut length) },
        0,
        "getsockopt: {}",
        std::io::Error::last_os_error()
    );
    assert_eq!(length, 4);
    value
}

fn tune_tcp(socket: &impl AsRawFd) {
    for (option, value) in [(7, 4096), (8, 16384)] {
        set(socket, 1, option, 0);
        assert!(get(socket, 1, option) > 0);
        set(socket, 1, option, -1);
        assert!(get(socket, 1, option) >= 65536);
        set(socket, 1, option, value);
        assert_eq!(get(socket, 1, option), value * 2);
        let invalid = 1;
        assert_eq!(
            unsafe { setsockopt(socket.as_raw_fd(), 1, option, &invalid, 1) },
            -1
        );
        assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(22));
        assert_eq!(get(socket, 1, option), value * 2);
    }
    set(socket, 1, 9, 0);
    for (option, value, max) in [(4, 17, 32767), (5, 7, 32767), (6, 3, 127)] {
        set(socket, 6, option, value);
        assert_eq!(get(socket, 6, option), value);
        assert_eq!(get(socket, 1, 9), 0, "tuning must not enable keepalive");
        for bad in [-1, 0, max + 1] {
            assert_eq!(
                unsafe { setsockopt(socket.as_raw_fd(), 6, option, &bad, 4) },
                -1
            );
            assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(22));
            assert_eq!(get(socket, 6, option), value);
        }
    }
    set(socket, 1, 9, 1);
    assert_eq!(get(socket, 1, 9), 1);
}

fn tcp_options(kind: &str, peer: SocketAddr) {
    let mut stream = if kind.starts_with("tcp-accepted") {
        let listener = TcpListener::bind(if peer.is_ipv4() {
            "127.0.0.1:0"
        } else {
            "[::1]:0"
        })
        .unwrap();
        let inherited = kind == "tcp-accepted-inherited";
        if inherited {
            tune_tcp(&listener);
            set(&listener, 6, 1, 1);
        }
        listener.set_nonblocking(false).unwrap();
        let local = listener.local_addr().unwrap();
        let client = std::thread::spawn(move || {
            let mut socket = TcpStream::connect(local).unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut bytes = [0; 65536];
            socket.read_exact(&mut bytes).unwrap();
            socket.write_all(&bytes).unwrap();
        });
        let (mut accepted, _) = listener.accept().unwrap();
        // Query before any custom option to cover the ordinary accepted socket.
        assert_eq!(accepted.nodelay().unwrap(), inherited);
        if inherited {
            assert_eq!(get(&accepted, 1, 7), 8192);
            assert_eq!(get(&accepted, 1, 8), 32768);
            assert_eq!(get(&accepted, 1, 9), 1);
            for (option, value) in [(4, 17), (5, 7), (6, 3)] {
                assert_eq!(get(&accepted, 6, option), value);
            }
            set(&accepted, 6, 4, 31);
            assert_eq!(get(&accepted, 6, 4), 31);
            assert_eq!(
                get(&listener, 6, 4),
                17,
                "accepted options must not mutate listener state"
            );
        }
        if !inherited {
            tune_tcp(&accepted);
        }
        accepted
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        accepted
            .set_write_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        accepted.set_nodelay(true).unwrap();
        assert!(accepted.nodelay().unwrap());
        accepted.write_all(&[0x53; 65536]).unwrap();
        let mut bytes = [0; 65536];
        accepted.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, &[0x53; 65536]);
        client.join().unwrap();
        return;
    } else if kind == "tcp-preconnect" {
        let fd = unsafe { raw_socket(if peer.is_ipv4() { 2 } else { 10 }, 1, 0) };
        assert!(fd >= 0);
        let socket = unsafe { TcpStream::from_raw_fd(fd) };
        tune_tcp(&socket);
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
        socket
    } else {
        let socket = TcpStream::connect(peer).unwrap();
        tune_tcp(&socket);
        socket
    };
    for (option, value) in [(4, 17), (5, 7), (6, 3)] {
        assert_eq!(get(&stream, 6, option), value);
    }
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    stream.write_all(&[0x53; 65536]).unwrap();
    let mut bytes = [0; 65536];
    stream.read_exact(&mut bytes).unwrap();
    assert_eq!(&bytes, &[0x53; 65536]);
}

fn check(kind: &str, peer: SocketAddr) {
    if kind.starts_with("tcp-") {
        return tcp_options(kind, peer);
    }
    let local = if peer.is_ipv4() {
        "0.0.0.0:0"
    } else {
        "[::]:0"
    };
    let socket = if kind == "ttl-prebind" {
        let fd = unsafe { raw_socket(if peer.is_ipv4() { 2 } else { 10 }, 2, 0) };
        assert!(fd >= 0);
        let socket = unsafe { UdpSocket::from_raw_fd(fd) };
        if peer.is_ipv4() {
            socket.set_ttl(37).unwrap();
        } else {
            set(&socket, 41, 16, 37);
        }
        let mut addr = [0u8; 28];
        let family = if peer.is_ipv4() { 2u16 } else { 10u16 };
        addr[..2].copy_from_slice(&family.to_ne_bytes());
        assert_eq!(
            unsafe { bind(fd, addr.as_ptr(), if peer.is_ipv4() { 16 } else { 28 }) },
            0
        );
        socket
    } else {
        UdpSocket::bind(local).unwrap()
    };
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    match kind {
        "ttl-disconnect" => {
            if peer.is_ipv4() {
                socket.set_ttl(37).unwrap();
            } else {
                set(&socket, 41, 16, 37);
            }
            for pass in 0..2 {
                socket.connect(peer).unwrap();
                socket.send(b"ttl").unwrap();
                let mut result = [0; 4];
                assert_eq!(socket.recv(&mut result).unwrap(), 4);
                assert_eq!(
                    i32::from_le_bytes(result),
                    37,
                    "hop limit after reconnect {pass}"
                );
                let address = [0u8; 2];
                assert_eq!(
                    unsafe { connect(socket.as_raw_fd(), address.as_ptr(), 2) },
                    0
                );
            }
        }
        "ttl-bound" | "ttl-connected" => {
            if kind == "ttl-connected" {
                socket.connect(peer).unwrap();
            }
            for ttl in [37, 83] {
                if peer.is_ipv4() {
                    socket.set_ttl(ttl).unwrap();
                } else {
                    set(&socket, 41, 16, ttl as i32);
                }
                socket.send_to(b"ttl", peer).unwrap();
                let mut result = [0u8; 4];
                let (n, _) = socket.recv_from(&mut result).unwrap();
                assert_eq!(n, 4);
                assert_eq!(
                    i32::from_le_bytes(result),
                    ttl as i32,
                    "actual packet hop limit"
                );
            }
        }
        "ttl-prebind" | "ttl-invalid" => {
            if kind == "ttl-invalid" {
                let (level, option) = if peer.is_ipv4() { (0, 2) } else { (41, 16) };
                set(&socket, level, option, 37);
                for value in [-2, 256] {
                    assert_eq!(
                        unsafe { setsockopt(socket.as_raw_fd(), level, option, &value, 4) },
                        -1
                    );
                    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(22));
                }
            }
            socket.send_to(b"ttl", peer).unwrap();
            let mut result = [0; 4];
            assert_eq!(socket.recv(&mut result).unwrap(), 4);
            assert_eq!(i32::from_le_bytes(result), 37);
        }
        "v6only-bound" => {
            let enabled = 1;
            assert_eq!(
                unsafe { setsockopt(socket.as_raw_fd(), 41, 26, &enabled, 4) },
                -1
            );
            assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(22));
        }
        "shared-mixed" => {
            set(&socket, 1, 2, 1);
            let second = shared_socket(false, socket.local_addr().unwrap().port(), false);
            let sender = UdpSocket::bind("127.0.0.1:0").unwrap();
            let destination =
                SocketAddr::from(([127, 0, 0, 1], socket.local_addr().unwrap().port()));
            sender.send_to(b"ipv4-priority", destination).unwrap();
            receive(&socket, b"ipv4-priority");
            second.set_nonblocking(true).unwrap();
            let mut data = [0; 32];
            assert_eq!(
                second.recv(&mut data).unwrap_err().kind(),
                std::io::ErrorKind::WouldBlock
            );
            second.set_nonblocking(false).unwrap();
            drop(socket);
            sender.send_to(b"ipv6-fallback", destination).unwrap();
            let (n, source) = second.recv_from(&mut data).unwrap();
            assert_eq!(&data[..n], b"ipv6-fallback");
            assert!(source.is_ipv6(), "{source}");
            assert_eq!(source.port(), sender.local_addr().unwrap().port());
        }
        "shared-connected" => {
            set(&socket, 1, 2, 1);
            let second = shared_socket(peer.is_ipv4(), socket.local_addr().unwrap().port(), false);
            let sender = UdpSocket::bind(SocketAddr::new(peer.ip(), 0)).unwrap();
            let alternate = UdpSocket::bind(SocketAddr::new(peer.ip(), 0)).unwrap();
            socket.connect(sender.local_addr().unwrap()).unwrap();
            let destination = SocketAddr::new(peer.ip(), socket.local_addr().unwrap().port());
            sender.send_to(b"before-change", destination).unwrap();
            socket.connect(alternate.local_addr().unwrap()).unwrap();
            receive(&socket, b"before-change");
            sender.send_to(b"old-peer", destination).unwrap();
            receive(&second, b"old-peer");
            alternate.send_to(b"new-peer", destination).unwrap();
            receive(&socket, b"new-peer");
            drop(socket);
            alternate.send_to(b"after-close", destination).unwrap();
            receive(&second, b"after-close");
        }
        "shared-reuseport" => {
            set(&socket, 1, 2, 1);
            set(&socket, 1, 15, 1);
            let second = shared_socket(peer.is_ipv4(), socket.local_addr().unwrap().port(), true);
            let destination = SocketAddr::new(peer.ip(), socket.local_addr().unwrap().port());
            socket.set_nonblocking(true).unwrap();
            second.set_nonblocking(true).unwrap();
            let mut counts = [0; 2];
            for _ in 0..40 {
                let sender = UdpSocket::bind(SocketAddr::new(peer.ip(), 0)).unwrap();
                let mut selected = None;
                for _ in 0..3 {
                    sender.send_to(b"flow", destination).unwrap();
                    let start = std::time::Instant::now();
                    loop {
                        let mut found = false;
                        for (index, receiver) in [&socket, &second].iter().enumerate() {
                            let mut data = [0; 16];
                            match receiver.recv(&mut data) {
                                Ok(n) => {
                                    assert_eq!(&data[..n], b"flow");
                                    if let Some(old) = selected {
                                        assert_eq!(old, index, "flow moved between sockets");
                                    } else {
                                        selected = Some(index);
                                    }
                                    counts[index] += 1;
                                    found = true;
                                }
                                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                                Err(e) => panic!("{e}"),
                            }
                        }
                        if found {
                            break;
                        }
                        assert!(
                            start.elapsed() < Duration::from_secs(2),
                            "missing shared flow"
                        );
                        std::thread::sleep(Duration::from_millis(1));
                    }
                }
            }
            assert!(counts.iter().all(|count| *count > 0), "{counts:?}");
            assert_eq!(counts.iter().sum::<usize>(), 120);
        }
        "ttl-query" => {
            socket.connect(peer).unwrap();
            socket.set_ttl(37).unwrap();
            assert_eq!(socket.ttl().unwrap(), 37);
        }
        "reuse" => {
            set(&socket, 1, 2, 1);
            // Use an unbound socket for the second bind, represented below by
            // a raw socket descriptor so SO_REUSEADDR precedes that bind.
            unsafe extern "C" {
                #[link_name = "socket"]
                fn raw_socket(domain: i32, kind: i32, protocol: i32) -> i32;
                fn bind(fd: i32, addr: *const u8, len: u32) -> i32;
            }
            let bound = socket.local_addr().unwrap();
            let fd = unsafe { raw_socket(if peer.is_ipv4() { 2 } else { 10 }, 2, 0) };
            assert!(fd >= 0);
            let owned = unsafe { OwnedFd::from_raw_fd(fd) };
            let enabled = 1;
            assert_eq!(unsafe { setsockopt(fd, 1, 2, &enabled, 4) }, 0);
            let mut addr = [0u8; 28];
            let family = if peer.is_ipv4() { 2u16 } else { 10u16 };
            addr[..2].copy_from_slice(&family.to_ne_bytes());
            addr[2..4].copy_from_slice(&bound.port().to_be_bytes());
            let ret = unsafe { bind(fd, addr.as_ptr(), if peer.is_ipv4() { 16 } else { 28 }) };
            let err = std::io::Error::last_os_error();
            assert_eq!(ret, 0, "second bind: {err}");
            let second = UdpSocket::from(owned);
            second
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let sender = UdpSocket::bind(local).unwrap();
            let destination = SocketAddr::new(peer.ip(), bound.port());
            sender.send_to(b"shared", destination).unwrap();
            let mut bytes = [0; 16];
            let (n, _) = second.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..n], b"shared");
            socket.set_nonblocking(true).unwrap();
            assert_eq!(
                socket.recv(&mut bytes).unwrap_err().kind(),
                std::io::ErrorKind::WouldBlock
            );
            socket.set_nonblocking(false).unwrap();
            drop(second);
            sender.send_to(b"after", destination).unwrap();
            let (n, _) = socket.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..n], b"after");
        }
        "broadcast" => {
            socket.set_broadcast(true).unwrap();
            assert!(socket.broadcast().unwrap());
            socket.connect("255.255.255.255:9").unwrap();
            socket.set_broadcast(false).unwrap();
            assert!(!socket.broadcast().unwrap());
            assert_eq!(
                socket
                    .connect("255.255.255.255:9")
                    .unwrap_err()
                    .raw_os_error(),
                Some(13)
            );
        }
        _ => panic!("unknown case"),
    }
}
fn main() {
    let config = fs::read_to_string("sockopt-config").unwrap();
    let mut failed = false;
    for line in config.lines() {
        let f: Vec<_> = line.split_whitespace().collect();
        let passed = std::panic::catch_unwind(|| check(f[1], f[2].parse().unwrap())).is_ok();
        println!(
            "SOCKOPT_CASE {} {}",
            f[0],
            if passed { "PASS" } else { "FAIL" }
        );
        failed |= !passed;
    }
    if failed {
        std::process::exit(1);
    }
}
