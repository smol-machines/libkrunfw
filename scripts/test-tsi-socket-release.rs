// Run as root inside a TSI guest with CONFIG_SLUB_DEBUG=y.
// Compile for the guest architecture with rustc, using a static musl target.
use std::{fs, net::UdpSocket, path::Path, thread, time::Duration};

fn objects() -> usize {
    // Allow deferred frees to run, then reclaim empty slab pages so cached
    // objects do not hide the per-socket control-channel leak.
    thread::sleep(Duration::from_millis(100));
    fs::write("/sys/kernel/slab/AF_VSOCK/shrink", "1").unwrap();
    fs::read_to_string("/sys/kernel/slab/AF_VSOCK/objects")
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .parse()
        .unwrap()
}

fn main() {
    if !Path::new("/sys/kernel/slab/AF_VSOCK/objects").exists() {
        eprintln!("Requires a TSI guest with CONFIG_SLUB_DEBUG=y");
        std::process::exit(77);
    }
    let before = objects();
    for _ in 0..500 {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        drop(socket);
    }
    let after = objects();
    println!("AF_VSOCK objects before={before} after={after}");
    assert_eq!(before, after, "closing UDP sockets retained VSOCK objects");
}
