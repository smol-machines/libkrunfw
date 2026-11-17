//! Compare connected UDP failure reporting with native Linux socket behavior.
use std::{
    fs, io,
    net::{SocketAddr, UdpSocket},
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    panic,
    time::Duration,
};

#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}
#[cfg_attr(target_arch = "x86_64", repr(C, packed))]
#[cfg_attr(not(target_arch = "x86_64"), repr(C))]
struct EpollEvent {
    events: u32,
    data: u64,
}
unsafe extern "C" {
    fn epoll_create1(flags: i32) -> i32;
    fn epoll_ctl(epfd: i32, op: i32, fd: i32, event: *mut EpollEvent) -> i32;
    fn epoll_wait(epfd: i32, events: *mut EpollEvent, count: i32, timeout: i32) -> i32;
    fn poll(fds: *mut PollFd, count: usize, timeout: i32) -> i32;
}

fn case(fields: &[&str]) {
    let peer: SocketAddr = fields[2].parse().unwrap();
    let socket = UdpSocket::bind(if peer.is_ipv4() {
        "0.0.0.0:0"
    } else {
        "[::]:0"
    })
    .unwrap();
    socket
        .set_read_timeout(Some(Duration::from_millis(400)))
        .unwrap();
    let epoll = if fields[1] == "epoll" {
        let fd = unsafe { epoll_create1(0x80000) };
        assert!(fd >= 0);
        let owned = unsafe { OwnedFd::from_raw_fd(fd) };
        let mut event = EpollEvent {
            events: 1,
            data: 42,
        };
        assert_eq!(
            unsafe { epoll_ctl(fd, 1, socket.as_raw_fd(), &mut event) },
            0
        );
        Some(owned)
    } else {
        None
    };
    socket.connect(peer).unwrap();
    assert_eq!(socket.send(b"closed-port").unwrap(), 11);
    let mut bytes = [0; 32];
    if let Some(epoll) = epoll {
        let mut event = EpollEvent { events: 0, data: 0 };
        assert_eq!(
            unsafe { epoll_wait(epoll.as_raw_fd(), &mut event, 1, 400) },
            1
        );
        let flags = event.events;
        let data = event.data;
        assert_ne!(flags & 8, 0);
        assert_eq!(data, 42);
        assert_eq!(
            socket.take_error().unwrap().unwrap().raw_os_error(),
            Some(111)
        );
        assert_eq!(
            unsafe { epoll_wait(epoll.as_raw_fd(), &mut event, 1, 0) },
            0
        );
    } else if fields[1] == "recv" {
        assert_eq!(
            socket.recv(&mut bytes).unwrap_err().raw_os_error(),
            Some(111)
        );
    } else {
        let mut fd = PollFd {
            fd: socket.as_raw_fd(),
            events: 1,
            revents: 0,
        };
        assert_eq!(
            unsafe { poll(&mut fd, 1, 400) },
            1,
            "closed UDP port did not wake poll"
        );
        assert_ne!(fd.revents & 8, 0, "expected POLLERR, got {}", fd.revents);
        match fields[1] {
            "so-error" => {
                assert_eq!(
                    socket.take_error().unwrap().unwrap().raw_os_error(),
                    Some(111)
                );
                assert!(socket.take_error().unwrap().is_none());
                assert_eq!(
                    unsafe { poll(&mut fd, 1, 0) },
                    0,
                    "consumed error remained ready"
                );
            }
            "send" => {
                assert_eq!(socket.send(b"next").unwrap_err().raw_os_error(), Some(111));
            }
            "nonblocking" => {
                socket.set_nonblocking(true).unwrap();
                assert_eq!(
                    socket.recv(&mut bytes).unwrap_err().raw_os_error(),
                    Some(111)
                );
            }
            other => panic!("unknown case: {other}"),
        }
    }
    assert!(
        socket.take_error().unwrap().is_none(),
        "error was reported twice"
    );
    socket.set_nonblocking(true).unwrap();
    assert_eq!(
        socket.recv(&mut bytes).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
}
fn main() {
    let config = fs::read_to_string("udp-error-config").unwrap();
    let mut failed = 0;
    for line in config.lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        let passed = panic::catch_unwind(|| case(&fields)).is_ok();
        println!(
            "UDP_ERROR_CASE {} {}",
            fields[0],
            if passed { "PASS" } else { "FAIL" }
        );
        failed += usize::from(!passed);
    }
    if failed != 0 {
        std::process::exit(1);
    }
}
