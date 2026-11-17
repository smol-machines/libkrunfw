use std::{
    fs,
    io::{Read, Write},
    net::{IpAddr, TcpStream},
    os::fd::FromRawFd,
    thread,
    time::{Duration, Instant},
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
#[repr(C)]
struct Timeval {
    sec: i64,
    us: i64,
}
unsafe extern "C" {
    fn socket(domain: i32, kind: i32, protocol: i32) -> i32;
    fn connect(fd: i32, addr: *const u8, len: u32) -> i32;
    fn poll(fds: *mut PollFd, n: usize, timeout: i32) -> i32;
    fn getsockopt(fd: i32, level: i32, name: i32, value: *mut i32, len: *mut u32) -> i32;
    fn setsockopt(fd: i32, level: i32, name: i32, value: *const Timeval, len: u32) -> i32;
    fn getpeername(fd: i32, addr: *mut u8, len: *mut u32) -> i32;
    fn getsockname(fd: i32, addr: *mut u8, len: *mut u32) -> i32;
    fn send(fd: i32, data: *const u8, len: usize, flags: i32) -> isize;
    fn recv(fd: i32, data: *mut u8, len: usize, flags: i32) -> isize;
    fn close(fd: i32) -> i32;
    fn shutdown(fd: i32, how: i32) -> i32;
    fn bind(fd: i32, addr: *const u8, len: u32) -> i32;
    fn listen(fd: i32, backlog: i32) -> i32;
    fn epoll_create1(flags: i32) -> i32;
    fn epoll_ctl(ep: i32, op: i32, fd: i32, event: *const EpollEvent) -> i32;
    fn epoll_wait(ep: i32, event: *mut EpollEvent, n: i32, timeout: i32) -> i32;
    fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    fn siginterrupt(sig: i32, flag: i32) -> i32;
    fn alarm(sec: u32) -> u32;
}
fn errno() -> i32 {
    std::io::Error::last_os_error().raw_os_error().unwrap()
}
struct Sock {
    i: i32,
    addr: [u8; 28],
    len: u32,
}
impl Drop for Sock {
    fn drop(&mut self) {
        unsafe { close(self.i) };
    }
}
impl Sock {
    fn new(ip: IpAddr, nb: bool) -> Self {
        let family: u16 = if ip.is_ipv4() { 2 } else { 10 };
        let mut addr = [0; 28];
        addr[..2].copy_from_slice(&family.to_ne_bytes());
        addr[2..4].copy_from_slice(&18443u16.to_be_bytes());
        let len = match ip {
            IpAddr::V4(a) => {
                addr[4..8].copy_from_slice(&a.octets());
                16
            }
            IpAddr::V6(a) => {
                addr[8..24].copy_from_slice(&a.octets());
                28
            }
        };
        let i = unsafe { socket(family.into(), 1 | if nb { 0x800 } else { 0 }, 0) };
        assert!(i >= 0);
        let binding = fs::read_to_string("/bind").unwrap_or_else(|_| "none".into());
        assert!(matches!(binding.trim(), "none" | "loopback" | "wildcard"));
        if binding.trim() != "none" {
            let mut local = addr;
            local[2..4].fill(0);
            if binding.trim() == "wildcard" {
                local[4..].fill(0);
            }
            assert_eq!(unsafe { bind(i, local.as_ptr(), len) }, 0);
        }
        Self { i, addr, len }
    }
    fn connect(&self) -> i32 {
        let r = unsafe { connect(self.i, self.addr.as_ptr(), self.len) };
        if r < 0 {
            -errno()
        } else {
            r
        }
    }
    fn error(&self) -> i32 {
        let mut e = 0;
        let mut n = 4;
        assert_eq!(unsafe { getsockopt(self.i, 1, 4, &mut e, &mut n) }, 0);
        e
    }
    fn ready(&self, ms: i32) -> i16 {
        let mut p = PollFd {
            fd: self.i,
            events: 4,
            revents: 0,
        };
        let rc = unsafe { poll(&mut p, 1, ms) };
        assert!(rc >= 0);
        p.revents
    }
    fn timeout(&self) {
        let t = Timeval { sec: 0, us: 100000 };
        assert_eq!(unsafe { setsockopt(self.i, 1, 21, &t, 16) }, 0);
    }
    fn echo(self) {
        let mut stream = unsafe { TcpStream::from_raw_fd(self.i) };
        std::mem::forget(self);
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        stream.write_all(b"PING").unwrap();
        let mut p = [0; 4];
        stream.read_exact(&mut p).unwrap();
        assert_eq!(&p, b"PONG");
    }
}
fn mark(name: &str) {
    fs::write(format!("/{name}"), b"1").unwrap();
}
fn wait(name: &str) {
    let start = Instant::now();
    while !std::path::Path::new(&format!("/{name}")).exists() {
        assert!(
            start.elapsed() < Duration::from_secs(5),
            "waiting for {name}"
        );
        thread::sleep(Duration::from_millis(2));
    }
}
fn setup(case: &str) {
    for n in ["release", "accepted", "closed"] {
        let _ = fs::remove_file(format!("/{n}"));
    }
    fs::write("/case", case).unwrap();
}
extern "C" fn on_alarm(_: i32) {}
fn main() {
    for text in [fs::read_to_string("/ip").unwrap().trim()] {
        let ip: IpAddr = text.parse().unwrap();
        setup("success");
        let s = Sock::new(ip, true);
        let mut before = [0; 128];
        let mut before_len = 128;
        assert_eq!(
            unsafe { getsockname(s.i, before.as_mut_ptr(), &mut before_len) },
            0
        );
        let bound_port = u16::from_be_bytes([before[2], before[3]]);
        assert_eq!(s.connect(), -115);
        wait("accepted");
        assert_eq!(s.ready(0), 0);
        assert_eq!(s.error(), 0);
        assert_eq!(s.connect(), -114);
        assert_eq!(unsafe { listen(s.i, 8) }, -1);
        assert_eq!(errno(), 22);
        assert_eq!(unsafe { bind(s.i, s.addr.as_ptr(), s.len) }, -1);
        assert_eq!(errno(), 22);
        let mut a = [0; 128];
        let mut n = 128;
        assert_eq!(unsafe { getpeername(s.i, a.as_mut_ptr(), &mut n) }, -1);
        assert_eq!(errno(), 107);
        n = 128;
        assert_eq!(unsafe { getsockname(s.i, a.as_mut_ptr(), &mut n) }, 0);
        assert_eq!(
            u16::from_ne_bytes([a[0], a[1]]),
            if ip.is_ipv4() { 2 } else { 10 }
        );
        if bound_port != 0 {
            assert_eq!(u16::from_be_bytes([a[2], a[3]]), bound_port);
            if fs::read_to_string("/bind").unwrap().trim() == "loopback" {
                let range = if ip.is_ipv4() { 4..8 } else { 8..24 };
                assert_eq!(a[range.clone()], before[range]);
            }
        }

        assert_eq!(unsafe { send(s.i, b"x".as_ptr(), 1, 0x40 | 0x4000) }, -1);
        assert_eq!(errno(), 11);
        assert_eq!(unsafe { recv(s.i, a.as_mut_ptr(), 1, 0x40) }, -1);
        assert_eq!(errno(), 11);
        let ep = unsafe { epoll_create1(0) };
        assert!(ep >= 0);
        let mut ev = EpollEvent {
            events: 1 | 4 | 0x80000000,
            data: 123,
        };
        assert_eq!(unsafe { epoll_ctl(ep, 1, s.i, &ev) }, 0);
        assert_eq!(unsafe { epoll_wait(ep, &mut ev, 1, 0) }, 0);
        mark("release");
        assert_eq!(unsafe { epoll_wait(ep, &mut ev, 1, 3000) }, 1);
        let events = ev.events;
        let data = ev.data;
        assert_ne!(events & 4, 0);
        assert_eq!(data, 123);
        assert_eq!(s.error(), 0);
        assert_eq!(s.connect(), 0);
        assert_eq!(s.connect(), -106);
        assert_eq!(unsafe { send(s.i, b"PING".as_ptr(), 4, 0x4000) }, 4);
        let start = Instant::now();
        loop {
            assert_eq!(
                unsafe { epoll_wait(ep, &mut ev, 1, 3000) },
                1,
                "connected read did not wake existing epoll registration"
            );
            let events = ev.events;
            if events & 1 != 0 {
                break;
            }
            assert!(start.elapsed() < Duration::from_secs(3));
        }
        let mut stream = unsafe { TcpStream::from_raw_fd(s.i) };
        std::mem::forget(s);
        stream.set_nonblocking(false).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut reply = [0; 4];
        stream.read_exact(&mut reply).unwrap();
        assert_eq!(&reply, b"PONG");
        unsafe { close(ep) };
        drop(stream);
        wait("closed");
        println!("ASYNC {text} delayed success, epoll edge, repeat connect, address queries, pending IO passed");
        setup("error");
        let s = Sock::new(ip, true);
        assert_eq!(s.connect(), -115);
        wait("accepted");
        mark("release");
        assert_ne!(s.ready(3000) & 8, 0);
        assert_eq!(s.error(), 111);
        assert_eq!(s.error(), 0);
        assert_eq!(s.ready(0) & 8, 0);
        drop(s);
        wait("closed");
        println!("ASYNC {text} delayed refusal and consumable SO_ERROR passed");
        setup("timeout");
        let s = Sock::new(ip, false);
        s.timeout();
        let started = Instant::now();
        assert_eq!(s.connect(), -115);
        assert!(started.elapsed() < Duration::from_secs(2));
        wait("accepted");
        mark("release");
        assert_ne!(s.ready(3000) & 4, 0);
        assert_eq!(s.error(), 0);
        s.echo();
        wait("closed");
        println!("ASYNC {text} blocking send timeout and eventual completion passed");
        setup("blocking");
        let s = Sock::new(ip, false);
        let gate = thread::spawn(|| {
            wait("accepted");
            thread::sleep(Duration::from_millis(50));
            mark("release");
        });
        assert_eq!(s.connect(), 0);
        gate.join().unwrap();
        s.echo();
        wait("closed");
        println!("ASYNC {text} blocking connect passed");
        setup("shutdown");
        let s = Sock::new(ip, true);
        assert_eq!(s.connect(), -115);
        wait("accepted");
        assert_eq!(unsafe { shutdown(s.i, 3) }, -1);
        assert_eq!(errno(), 22);
        assert_eq!(unsafe { shutdown(s.i, 2) }, 0);
        assert_ne!(s.ready(0) & 8, 0);
        assert_eq!(s.error(), 104);
        drop(s);
        wait("closed");
        println!("ASYNC {text} shutdown cancels pending host dial passed");
        setup("signal");
        let s = Sock::new(ip, false);
        unsafe {
            signal(14, on_alarm);
            siginterrupt(14, 1);
            alarm(1);
        };
        assert_eq!(s.connect(), -4);
        unsafe { alarm(0) };
        drop(s);
        wait("closed");
        println!("ASYNC {text} signal interrupts blocking connect passed");
        for _ in 0..12 {
            setup("cancel");
            let s = Sock::new(ip, true);
            assert_eq!(s.connect(), -115);
            wait("accepted");
            drop(s);
            wait("closed");
        }
        println!("ASYNC {text} 12 pending connects closed and host connections released passed");
        for _ in 0..32 {
            setup("race");
            let s = Sock::new(ip, true);
            assert_eq!(s.connect(), -115);
            wait("accepted");
            mark("release");
            drop(s);
            wait("closed");
        }
        println!("ASYNC {text} 32 close-versus-completion races passed");
    }
    println!("ASYNC_CONNECT_VM_PASS");
}
