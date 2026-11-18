use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};
fn main() {
    let listeners = [
        TcpListener::bind(("127.0.0.1", 0)).unwrap(),
        TcpListener::bind(("::1", 0)).unwrap(),
    ];
    let addresses = listeners
        .iter()
        .map(|l| l.local_addr().unwrap().to_string())
        .collect::<Vec<_>>();
    let counter = Arc::new(AtomicU64::new(0));
    let workers = listeners
        .into_iter()
        .map(|listener| {
            let counter = counter.clone();
            std::thread::spawn(move || loop {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = [0; 1];
                stream.read_exact(&mut request).unwrap();
                let value = counter.fetch_add(1, Ordering::SeqCst) + 1;
                stream.write_all(&value.to_le_bytes()).unwrap();
            })
        })
        .collect::<Vec<_>>();
    fs::write("/listener-ready", addresses.join("\n")).unwrap();
    for worker in workers {
        worker.join().unwrap();
    }
}
