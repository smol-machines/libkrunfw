// Run inside a TSI guest with unrestricted port exposure and host loopback access.
// Compile for the guest architecture with rustc using a static musl target.
use std::{
    io::{ErrorKind, Read, Write},
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, TcpListener, TcpStream},
    thread,
    time::{Duration, Instant},
};

fn main() {
    thread::spawn(|| {
        thread::sleep(Duration::from_secs(15));
        eprintln!("listener regression timed out");
        std::process::exit(124);
    });
    for ip in [
        IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(Ipv6Addr::LOCALHOST),
        IpAddr::V4(Ipv4Addr::UNSPECIFIED),
        IpAddr::V6(Ipv6Addr::UNSPECIFIED),
    ] {
        let listener = TcpListener::bind((ip, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        assert_ne!(address.port(), 0);
        let loopback = if ip.is_ipv4() {
            IpAddr::V4(Ipv4Addr::LOCALHOST)
        } else {
            IpAddr::V6(Ipv6Addr::LOCALHOST)
        };
        let client = thread::spawn(move || {
            let mut stream = TcpStream::connect(SocketAddr::new(loopback, address.port())).unwrap();
            stream.write_all(b"ping").unwrap();
            let mut reply = [0; 4];
            stream.read_exact(&mut reply).unwrap();
            assert_eq!(&reply, b"pong");
        });
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut request = [0; 4];
        stream.read_exact(&mut request).unwrap();
        assert_eq!(&request, b"ping");
        stream
            .set_read_timeout(Some(Duration::from_millis(20)))
            .unwrap();
        assert_eq!(
            stream.read_timeout().unwrap(),
            Some(Duration::from_millis(20))
        );
        let start = Instant::now();
        let error = stream.read(&mut [0; 1]).unwrap_err();
        assert!(matches!(
            error.kind(),
            ErrorKind::WouldBlock | ErrorKind::TimedOut
        ));
        assert!(start.elapsed() >= Duration::from_millis(10));
        assert!(start.elapsed() < Duration::from_secs(1));
        stream.write_all(b"pong").unwrap();
        client.join().unwrap();
        assert_eq!(listener.local_addr().unwrap(), address);
        println!("listener {address}: connection and accepted read timeout passed");
    }
}
