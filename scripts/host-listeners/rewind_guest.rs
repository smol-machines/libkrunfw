use std::{
    fs,
    io::{ErrorKind, Read, Write},
    net::TcpListener,
    os::unix::fs::OpenOptionsExt,
    time::Duration,
};

fn watcher() -> fs::File {
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(0o4000) // Linux O_NONBLOCK.
        .open("/proc/smolvm-listeners")
        .unwrap()
}
fn generation(watcher: &mut fs::File) -> u64 {
    let mut bytes = [0; 8];
    match watcher.read(&mut bytes) {
        Ok(8) => u64::from_le_bytes(bytes),
        Err(error) if error.kind() == ErrorKind::WouldBlock => u64::MAX,
        result => panic!("listener generation: {result:?}"),
    }
}

fn listen(ip: &str) -> TcpListener {
    let socket = TcpListener::bind((ip, 0)).unwrap();
    socket.set_nonblocking(true).unwrap();
    socket
}

fn main() {
    if !std::path::Path::new("/proc/smolvm-listeners").exists() {
        unsafe extern "C" {
            fn mount(
                source: *const std::ffi::c_char,
                target: *const std::ffi::c_char,
                kind: *const std::ffi::c_char,
                flags: usize,
                data: *const std::ffi::c_void,
            ) -> i32;
        }
        fs::create_dir_all("/proc").unwrap();
        assert_eq!(
            unsafe {
                mount(
                    c"proc".as_ptr(),
                    c"/proc".as_ptr(),
                    c"proc".as_ptr(),
                    0,
                    std::ptr::null(),
                )
            },
            0
        );
    }
    let mut listeners = vec![Some(listen("127.0.0.1")), Some(listen("::1"))];
    let mut notifications = watcher();
    fs::write(
        "/listener-ready",
        listeners
            .iter()
            .map(|s| s.as_ref().unwrap().local_addr().unwrap().to_string())
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .unwrap();
    let mut counter = 0_u64;
    loop {
        for index in 0..listeners.len() {
            let Some(listener) = &listeners[index] else {
                continue;
            };
            let mut stream = match listener.accept() {
                Ok((stream, _)) => stream,
                Err(error) if error.kind() == ErrorKind::WouldBlock => continue,
                Err(error) => panic!("accept: {error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = [0];
            if stream.read_exact(&mut request).is_err() {
                continue;
            }
            let value = match request[0] {
                b'G' => generation(&mut notifications),
                b'R' => generation(&mut watcher()),
                b'?' => {
                    counter += 1;
                    counter
                }
                b'N' => {
                    let listener = listen("127.0.0.1");
                    let port = u64::from(listener.local_addr().unwrap().port());
                    listeners.push(Some(listener));
                    port
                }
                b'D' => {
                    listeners[index] = None;
                    1
                }
                _ => panic!("invalid request"),
            };
            stream.write_all(&value.to_le_bytes()).unwrap();
        }
        std::thread::sleep(Duration::from_millis(5));
    }
}
