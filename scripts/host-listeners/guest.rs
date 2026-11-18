use std::{
    io::{Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    os::fd::AsRawFd,
};
unsafe extern "C" {
    fn getsockopt(fd: i32, level: i32, name: i32, value: *mut i32, size: *mut u32) -> i32;
    fn setsockopt(fd: i32, level: i32, name: i32, value: *const i32, size: u32) -> i32;
    fn socket(domain: i32, kind: i32, protocol: i32) -> i32;
    fn close(fd: i32) -> i32;
    fn listen(fd: i32, backlog: i32) -> i32;
    fn shutdown(fd: i32, how: i32) -> i32;
    fn accept(fd: i32, address: *mut u8, len: *mut u32) -> i32;
    fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    fn siginterrupt(sig: i32, flag: i32) -> i32;
    fn alarm(seconds: u32) -> u32;
    fn pthread_self() -> usize;
    fn pthread_kill(thread: usize, signal: i32) -> i32;
}
fn accepting(fd: i32) -> i32 {
    let mut value = -1;
    let mut size = 4;
    assert_eq!(unsafe { getsockopt(fd, 1, 30, &mut value, &mut size) }, 0);
    assert_eq!(size, 4);
    value
}

fn check_shutdown(ip: &str, mode: i32, fixed_port: bool) {
    use std::{
        sync::{mpsc, Arc},
        time::Duration,
    };
    let initial = TcpListener::bind((ip, 0)).unwrap();
    let listener = Arc::new(if fixed_port {
        let address = initial.local_addr().unwrap();
        drop(initial);
        TcpListener::bind(address).unwrap()
    } else {
        initial
    });
    let address = listener.local_addr().unwrap();
    let mut client = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let (mut established, _) = listener.accept().unwrap();
    let (ready, started) = mpsc::channel();
    let (completed, result) = mpsc::channel();
    let mut workers = Vec::new();
    for _ in 0..3 {
        let waiting = listener.clone();
        let ready = ready.clone();
        let completed = completed.clone();
        workers.push(std::thread::spawn(move || {
            ready.send(()).unwrap();
            completed.send(waiting.accept()).unwrap();
        }));
    }
    for _ in 0..3 {
        started.recv().unwrap();
    }
    std::thread::sleep(Duration::from_millis(30));
    let fd = listener.as_raw_fd();
    assert_eq!(
        unsafe { shutdown(fd, mode) },
        0,
        "{ip}: listener shutdown {mode}"
    );
    assert_eq!(accepting(fd), i32::from(mode == 1), "{ip}: shutdown {mode}");
    if mode == 1 {
        assert!(
            matches!(
                result.recv_timeout(Duration::from_millis(50)),
                Err(mpsc::RecvTimeoutError::Timeout)
            ),
            "write shutdown cancelled a pending accept"
        );
        for _ in 0..3 {
            let _second = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
            result
                .recv_timeout(Duration::from_secs(2))
                .unwrap()
                .unwrap();
        }
    } else {
        for _ in 0..3 {
            let error = result
                .recv_timeout(Duration::from_secs(2))
                .unwrap()
                .unwrap_err();
            assert_eq!(
                error.raw_os_error(),
                Some(22),
                "read shutdown must cancel every pending accept"
            );
        }
        let started = std::time::Instant::now();
        assert!(
            TcpStream::connect_timeout(&address, Duration::from_millis(200)).is_err(),
            "read shutdown left the host listener accepting connections"
        );
        assert!(
            started.elapsed() < Duration::from_secs(1),
            "connect_timeout blocked beyond its deadline: {:?}",
            started.elapsed()
        );
    }
    for worker in workers {
        worker.join().unwrap();
    }
    established.write_all(b"alive").unwrap();
    let mut bytes = [0; 5];
    client.read_exact(&mut bytes).unwrap();
    assert_eq!(
        &bytes, b"alive",
        "listener shutdown closed an accepted connection"
    );
    assert_eq!(unsafe { listen(fd, 32) }, 0, "listen after shutdown");
    let next_address = listener.local_addr().unwrap();
    if fixed_port {
        assert_eq!(next_address, address, "explicit bind must keep its port");
    }
    let address = next_address;
    let mut next = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
    next.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let (mut accepted, _) = listener.accept().unwrap();
    accepted.write_all(b"again").unwrap();
    next.read_exact(&mut bytes).unwrap();
    assert_eq!(&bytes, b"again");
}

fn check_queued_shutdown(ip: &str) {
    use std::time::Duration;
    let listener = TcpListener::bind((ip, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let mut queued = Vec::new();
    for _ in 0..4 {
        let stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        queued.push(stream);
    }
    std::thread::sleep(Duration::from_millis(30));
    assert_eq!(unsafe { shutdown(listener.as_raw_fd(), 0) }, 0);
    for stream in &mut queued {
        let mut byte = [0];
        match stream.read(&mut byte) {
            Ok(0) => (),
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => (),
            other => panic!("unaccepted connection survived listener shutdown: {other:?}"),
        }
    }
    assert_eq!(unsafe { listen(listener.as_raw_fd(), 32) }, 0);
    let mut client =
        TcpStream::connect_timeout(&listener.local_addr().unwrap(), Duration::from_secs(2))
            .unwrap();
    client.write_all(b"fresh").unwrap();
    let (mut accepted, _) = listener.accept().unwrap();
    accepted
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut bytes = [0; 5];
    accepted.read_exact(&mut bytes).unwrap();
    assert_eq!(
        &bytes, b"fresh",
        "relisten returned an old queued connection"
    );
}

extern "C" fn on_alarm(_: i32) {}

fn check_interrupted_accept(ip: &str) {
    use std::time::Duration;
    let listener = TcpListener::bind((ip, 0)).unwrap();
    unsafe {
        signal(14, on_alarm);
        siginterrupt(14, 1);
        alarm(1);
    }
    let result = unsafe {
        accept(
            listener.as_raw_fd(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    let error = std::io::Error::last_os_error();
    unsafe { alarm(0) };
    assert_eq!(result, -1, "accept must be interrupted");
    assert_eq!(error.raw_os_error(), Some(4));
    assert_eq!(unsafe { shutdown(listener.as_raw_fd(), 0) }, 0);
    assert_eq!(unsafe { listen(listener.as_raw_fd(), 32) }, 0);
    let mut client =
        TcpStream::connect_timeout(&listener.local_addr().unwrap(), Duration::from_secs(2))
            .unwrap();
    client.write_all(b"after").unwrap();
    let fd = unsafe {
        accept(
            listener.as_raw_fd(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    assert!(
        fd >= 0,
        "stale interrupted-accept reply after relisten: {}",
        std::io::Error::last_os_error()
    );
    let mut accepted = unsafe { <TcpStream as std::os::fd::FromRawFd>::from_raw_fd(fd) };
    accepted
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut bytes = [0; 5];
    accepted.read_exact(&mut bytes).unwrap();
    assert_eq!(&bytes, b"after");
}

fn check_accept_timeout(ip: &str) {
    use std::time::{Duration, Instant};
    #[repr(C)]
    struct Timeval {
        sec: i64,
        us: i64,
    }
    let listener = TcpListener::bind((ip, 0)).unwrap();
    listener.set_nonblocking(true).unwrap();
    let started = Instant::now();
    assert_eq!(
        unsafe {
            accept(
                listener.as_raw_fd(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        -1
    );
    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(11));
    assert!(started.elapsed() < Duration::from_secs(1));
    listener.set_nonblocking(false).unwrap();
    let timeout = Timeval { sec: 0, us: 200000 };
    assert_eq!(
        unsafe {
            setsockopt(
                listener.as_raw_fd(),
                1,
                20,
                (&timeout as *const Timeval).cast(),
                16,
            )
        },
        0
    );
    let started = Instant::now();
    assert_eq!(
        unsafe {
            accept(
                listener.as_raw_fd(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        },
        -1
    );
    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(11));
    assert!(started.elapsed() >= Duration::from_millis(100));
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "accept ignored receive timeout"
    );
}

fn check_concurrent_interruption(ip: &str) {
    use std::{
        sync::{mpsc, Arc},
        time::Duration,
    };
    let listener = Arc::new(TcpListener::bind((ip, 0)).unwrap());
    let (ready, started) = mpsc::channel();
    let (completed, result) = mpsc::channel();
    let waiting = listener.clone();
    let worker = std::thread::spawn(move || {
        ready.send(()).unwrap();
        completed.send(waiting.accept()).unwrap();
    });
    started.recv().unwrap();
    std::thread::sleep(Duration::from_millis(30));
    let main_thread = unsafe { pthread_self() };
    let interrupt = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(unsafe { pthread_kill(main_thread, 14) }, 0);
    });
    let fd = unsafe {
        accept(
            listener.as_raw_fd(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    let error = std::io::Error::last_os_error();
    assert_eq!(fd, -1);
    assert_eq!(
        error.raw_os_error(),
        Some(4),
        "second waiting accept did not observe signal"
    );
    interrupt.join().unwrap();
    let mut client =
        TcpStream::connect_timeout(&listener.local_addr().unwrap(), Duration::from_secs(2))
            .unwrap();
    client.write_all(b"other").unwrap();
    let (mut accepted, _) = result
        .recv_timeout(Duration::from_secs(2))
        .unwrap()
        .unwrap();
    accepted
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut bytes = [0; 5];
    accepted.read_exact(&mut bytes).unwrap();
    assert_eq!(&bytes, b"other");
    worker.join().unwrap();
}

fn main() {
    let state_only = std::fs::read_to_string("listener-config").unwrap().trim() == "state";
    for (family, ip) in [(2, "127.0.0.1"), (10, "::1")] {
        let fresh = unsafe { socket(family, 1, 0) };
        assert!(fresh >= 0);
        assert_eq!(accepting(fresh), 0);
        assert_eq!(unsafe { close(fresh) }, 0);
        for mode in if state_only { vec![0] } else { vec![0, 1, 2] } {
            let listener = TcpListener::bind((ip, 0)).unwrap();
            let fd = listener.as_raw_fd();
            assert_eq!(accepting(fd), 1, "{ip}: successful listen is not reported");
            assert_eq!(unsafe { listen(fd, 32) }, 0);
            assert_eq!(accepting(fd), 1);
            let address = listener.local_addr().unwrap();
            let client = std::thread::spawn(move || {
                let mut stream = TcpStream::connect(address).unwrap();
                assert_eq!(accepting(stream.as_raw_fd()), 0);
                assert!(unsafe { listen(stream.as_raw_fd(), 1) } < 0);
                assert_eq!(accepting(stream.as_raw_fd()), 0);
                stream.write_all(b"request").unwrap();
                stream.shutdown(Shutdown::Write).unwrap();
                let mut reply = Vec::new();
                stream.read_to_end(&mut reply).unwrap();
                assert_eq!(reply, b"response");
            });
            let (mut stream, _) = listener.accept().unwrap();
            assert_eq!(accepting(stream.as_raw_fd()), 0);
            let mut request = Vec::new();
            stream.read_to_end(&mut request).unwrap();
            assert_eq!(request, b"request");
            stream.write_all(b"response").unwrap();
            stream.shutdown(Shutdown::Write).unwrap();
            client.join().unwrap();
            {
                let listener = std::sync::Arc::new(listener);
                let accepting = listener.clone();
                let (ready, started) = std::sync::mpsc::channel();
                let waiter = std::thread::spawn(move || {
                    ready.send(()).unwrap();
                    let (mut connection, _) = accepting.accept().unwrap();
                    connection.write_all(b"metadata").unwrap();
                });
                started.recv().unwrap();
                std::thread::sleep(std::time::Duration::from_millis(30));
                // This must finish while accept is waiting, before a client
                // connects; both used to receive from one control channel.
                assert_eq!(listener.local_addr().unwrap(), address);
                let enabled = 1;
                assert_eq!(unsafe { setsockopt(fd, 1, 9, &enabled, 4) }, 0);
                let mut actual = 0;
                let mut size = 4;
                assert_eq!(unsafe { getsockopt(fd, 1, 9, &mut actual, &mut size) }, 0);
                assert_eq!(actual, enabled);
                // listen() has its own synchronous reply. It must neither
                // consume the accept reply nor erase the host's pending waiter.
                assert_eq!(unsafe { listen(fd, 64) }, 0);
                let mut client = TcpStream::connect(address).unwrap();
                let mut reply = [0; 8];
                client.read_exact(&mut reply).unwrap();
                assert_eq!(&reply, b"metadata");
                waiter.join().unwrap();
            }
            if !state_only {
                check_shutdown(ip, mode, false);
                check_shutdown(ip, mode, true);
            }
        }
        if !state_only {
            check_queued_shutdown(ip);
            check_interrupted_accept(ip);
            check_accept_timeout(ip);
            check_concurrent_interruption(ip);
        }
        println!("LISTENER_RESULT {ip} passed");
    }
}
