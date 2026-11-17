use std::{
    fs,
    io::{Read, Write},
    net::{SocketAddr, TcpStream, UdpSocket},
    os::fd::{AsRawFd, FromRawFd},
    time::{Duration, Instant},
};

unsafe extern "C" {
    fn socket(family: i32, kind: i32, protocol: i32) -> i32;
    fn bind(fd: i32, address: *const u8, length: u32) -> i32;
    fn setsockopt(fd: i32, level: i32, name: i32, value: *const i32, len: u32) -> i32;
    fn getsockopt(fd: i32, level: i32, name: i32, value: *mut i32, len: *mut u32) -> i32;
}

fn receive_limit(socket: &UdpSocket) -> i32 {
    let mut value = 0;
    let mut length = 4;
    assert_eq!(
        unsafe { getsockopt(socket.as_raw_fd(), 1, 8, &mut value, &mut length) },
        0
    );
    assert_eq!(length, 4);
    value
}

fn tune(socket: &UdpSocket, value: i32, option: i32) -> i32 {
    assert_eq!(
        unsafe { setsockopt(socket.as_raw_fd(), 1, option, &value, 4) },
        0
    );
    let actual = receive_limit(socket);
    assert_eq!(actual, value * 2);
    // Invalid input must not change either query state or queue behavior.
    let invalid = 1;
    assert_eq!(
        unsafe { setsockopt(socket.as_raw_fd(), 1, option, &invalid, 1) },
        -1
    );
    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(22));
    assert_eq!(receive_limit(socket), actual);
    actual
}

fn main() {
    for line in fs::read_to_string("buffer-config").unwrap().lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        let value: i32 = fields[2].parse().unwrap();
        let option = if fields[1] == "forced" { 33 } else { 8 };
        let sender: SocketAddr = fields[4].parse().unwrap();
        let ipv4 = sender.is_ipv4();
        let fd = unsafe { socket(if ipv4 { 2 } else { 10 }, 2, 0) };
        assert!(fd >= 0);
        let data = unsafe { UdpSocket::from_raw_fd(fd) };
        if fields[1] == "prebind" {
            tune(&data, value, option);
        }
        let mut address = [0u8; 28];
        address[..2].copy_from_slice(&(if ipv4 { 2u16 } else { 10u16 }).to_ne_bytes());
        if ipv4 {
            address[4..8].copy_from_slice(&[127, 0, 0, 1]);
        } else {
            address[23] = 1;
        }
        assert_eq!(
            unsafe { bind(fd, address.as_ptr(), if ipv4 { 16 } else { 28 }) },
            0
        );
        if fields[1] == "connected" {
            data.connect(sender).unwrap();
        }
        if fields[1] != "prebind" {
            tune(&data, value, option);
        }
        data.set_nonblocking(true).unwrap();
        let mut control = TcpStream::connect(fields[3]).unwrap();
        control
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        writeln!(control, "{}", data.local_addr().unwrap()).unwrap();
        let mut ack = [0];
        control.read_exact(&mut ack).unwrap();
        let start = Instant::now();
        let mut received = 0;
        let mut packet = [0u8; 1024];
        let mut seen = [false; 512];
        while start.elapsed() < Duration::from_millis(200) {
            match data.recv_from(&mut packet) {
                Ok((n, source)) => {
                    assert_eq!(n, 1024);
                    assert_eq!(source, sender);
                    let sequence = u32::from_le_bytes(packet[..4].try_into().unwrap()) as usize;
                    assert!(sequence < 512 && !seen[sequence]);
                    assert!(packet[4..].iter().all(|byte| *byte == 0));
                    seen[sequence] = true;
                    received += 1;
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(1));
                }
                Err(e) => panic!("receive: {e}"),
            }
        }
        println!(
            "BUFFER_CASE {} {} {}",
            fields[0],
            receive_limit(&data),
            received
        );
    }
}
