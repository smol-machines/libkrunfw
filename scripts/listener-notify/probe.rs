use std::{
    fs::OpenOptions,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[repr(C)]
struct PollFd {
    fd: i32,
    events: i16,
    revents: i16,
}
unsafe extern "C" {
    fn poll(fds: *mut PollFd, count: usize, timeout: i32) -> i32;
    fn listen(fd: i32, backlog: i32) -> i32;
    fn shutdown(fd: i32, how: i32) -> i32;
    fn alarm(seconds: u32) -> u32;
    fn socket(family: i32, kind: i32, protocol: i32) -> i32;
    fn getsockname(fd: i32, address: *mut u64, length: *mut u32) -> i32;
    fn bind(fd: i32, address: *const u64, length: u32) -> i32;
    fn close(fd: i32) -> i32;
}
fn watch() -> std::fs::File {
    OpenOptions::new()
        .read(true)
        .custom_flags(0x800)
        .open("/proc/smolvm-listeners")
        .unwrap()
}
fn generation(file: &mut std::fs::File) -> u64 {
    let mut p = PollFd {
        fd: file.as_raw_fd(),
        events: 1,
        revents: 0,
    };
    assert_eq!(unsafe { poll(&mut p, 1, 2000) }, 1);
    assert_ne!(p.revents & 1, 0);
    let mut bytes = [0; 8];
    assert_eq!(file.read(&mut bytes).unwrap(), 8);
    u64::from_le_bytes(bytes)
}
#[track_caller]
fn quiet(file: &mut std::fs::File) {
    let mut p = PollFd {
        fd: file.as_raw_fd(),
        events: 1,
        revents: 0,
    };
    assert_eq!(unsafe { poll(&mut p, 1, 30) }, 0);
    assert_eq!(
        file.read(&mut [0; 8]).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
}
fn check(ip: &str) {
    let mut a = watch();
    let mut b = watch();
    // A failed short read must not consume the initial generation.
    assert_eq!(a.read(&mut [0; 1]).unwrap_err().raw_os_error(), Some(22));
    let before = generation(&mut a);
    assert_eq!(generation(&mut b), before);
    quiet(&mut a);
    let listener = TcpListener::bind((ip, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let listening = generation(&mut a);
    assert!(listening > before);
    assert_eq!(generation(&mut b), listening);
    assert_eq!(unsafe { listen(listener.as_raw_fd(), 32) }, 0);
    quiet(&mut a);
    // Use a socket without SO_REUSEADDR for an unambiguous failed bind.
    let mut raw = [0u64; 16];
    let mut length = std::mem::size_of_val(&raw) as u32;
    assert_eq!(
        unsafe { getsockname(listener.as_raw_fd(), raw.as_mut_ptr(), &mut length) },
        0
    );
    let failed = unsafe { socket(if address.is_ipv4() { 2 } else { 10 }, 1, 0) };
    assert!(failed >= 0);
    assert_eq!(unsafe { bind(failed, raw.as_ptr(), length) }, -1);
    assert_eq!(unsafe { close(failed) }, 0);
    quiet(&mut a);
    let mut client = TcpStream::connect(address).unwrap();
    let (mut accepted, _) = listener.accept().unwrap();
    let mut byte = [0];
    client.write_all(b"a").unwrap();
    accepted.read_exact(&mut byte).unwrap();
    assert_eq!(&byte, b"a");
    accepted.write_all(b"b").unwrap();
    client.read_exact(&mut byte).unwrap();
    assert_eq!(&byte, b"b");
    quiet(&mut a); // Accepted sockets and traffic must not invalidate discovery.
    assert_eq!(unsafe { shutdown(listener.as_raw_fd(), 1) }, 0);
    quiet(&mut a); // Write shutdown leaves a listener accepting.
    assert_eq!(unsafe { shutdown(listener.as_raw_fd(), 0) }, 0);
    let stopped = generation(&mut a);
    assert!(stopped > listening);
    assert_eq!(generation(&mut b), stopped);
    client.write_all(b"c").unwrap();
    accepted.read_exact(&mut byte).unwrap();
    assert_eq!(&byte, b"c");
    drop(client);
    drop(accepted);
    drop(listener);
    quiet(&mut a); // Already stopped listeners must not notify again.
    let listener = TcpListener::bind((ip, 0)).unwrap();
    let listening = generation(&mut a);
    drop(listener);
    let closed = generation(&mut a);
    assert!(closed > listening);
    assert_eq!(generation(&mut b), closed); // Independent cursor and burst coalescing.
    quiet(&mut a);
    println!("NOTIFY_RESULT {ip} passed");
}
fn main() {
    unsafe { alarm(20) };
    check("127.0.0.1");
    check("::1");
    std::fs::write("/sys/module/kernel/parameters/tsi_hijack", b"0").unwrap();
    check("127.0.0.1");
    check("::1");
    println!("NOTIFY_RESULT native passed");
}
