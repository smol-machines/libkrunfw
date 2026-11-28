//! Connected UDP probe for the legacy TSI transport, in and out of the guest's
//! initial network namespace.
use std::{
    fs,
    net::{SocketAddr, UdpSocket},
    os::fd::AsRawFd,
    panic, ptr,
    time::Duration,
};

const CLONE_NEWNET: i32 = 0x4000_0000;

#[repr(C)]
struct IoVec {
    base: *const u8,
    len: usize,
}

#[repr(C)]
struct MsgHdr {
    name: *const u8,
    name_len: u32,
    iov: *const IoVec,
    iov_len: usize,
    control: *const u8,
    control_len: usize,
    flags: i32,
}

#[repr(C)]
struct MMsgHdr {
    hdr: MsgHdr,
    len: u32,
}

unsafe extern "C" {
    fn sendmmsg(fd: i32, messages: *mut MMsgHdr, count: u32, flags: i32) -> i32;
    fn unshare(flags: i32) -> i32;
}

fn receive(socket: &UdpSocket) -> (Vec<u8>, SocketAddr) {
    let mut bytes = [0; 128];
    let (length, source) = socket.recv_from(&mut bytes).unwrap();
    (bytes[..length].to_vec(), source)
}

fn echo(socket: &UdpSocket, payload: &[u8]) -> SocketAddr {
    assert_eq!(socket.send(payload).unwrap(), payload.len());
    let (bytes, source) = receive(socket);
    assert_eq!(bytes, payload);
    source
}

fn case(kind: &str, peer: SocketAddr, alternate: SocketAddr) {
    let wildcard = if peer.is_ipv4() { "0.0.0.0:0" } else { "[::]:0" };
    let socket = UdpSocket::bind(wildcard).unwrap();
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    socket.connect(peer).unwrap();
    match kind {
        // Every datagram after the first still needs the connected peer.
        "repeat" => {
            for payload in [&b"first"[..], b"second", b"third"] {
                assert_eq!(echo(&socket, payload), peer);
            }
        }
        // glibc sends the A and AAAA queries of one lookup this way.
        "batch" => {
            let payloads = [&b"batch-a"[..], b"batch-b"];
            let vectors = payloads.map(|payload| IoVec {
                base: payload.as_ptr(),
                len: payload.len(),
            });
            let mut messages = [&vectors[0], &vectors[1]].map(|vector| MMsgHdr {
                hdr: MsgHdr {
                    name: ptr::null(),
                    name_len: 0,
                    iov: vector,
                    iov_len: 1,
                    control: ptr::null(),
                    control_len: 0,
                    flags: 0,
                },
                len: 0,
            });
            let sent = unsafe { sendmmsg(socket.as_raw_fd(), messages.as_mut_ptr(), 2, 0) };
            assert_eq!(sent, 2);
            assert_eq!(messages.map(|message| message.len), [7, 7]);
            let mut replies = [receive(&socket), receive(&socket)];
            replies.sort();
            assert_eq!(replies[0], (payloads[0].to_vec(), peer));
            assert_eq!(replies[1], (payloads[1].to_vec(), peer));
            assert_eq!(echo(&socket, b"after-batch"), peer);
        }
        // A send to another address must not replace the connected peer. The
        // alternate never answers; the host asserts where each datagram went.
        "alternate" => {
            echo(&socket, b"before");
            assert_eq!(socket.send_to(b"alternate", alternate).unwrap(), 9);
            echo(&socket, b"after-alternate");
            echo(&socket, b"after-alternate-again");
        }
        other => panic!("unknown test: {other}"),
    }
}

fn main() {
    let config = fs::read_to_string("dgram-config").unwrap();
    let mut failed = 0;
    let mut nested = false;
    for line in config.lines() {
        let fields: Vec<_> = line.split_whitespace().collect();
        assert_eq!(fields.len(), 5);
        // A fresh namespace has no route at all, like a container whose only
        // way out is the proxy. Cases are ordered so this happens once.
        if fields[1] == "nested" && !nested {
            assert_eq!(unsafe { unshare(CLONE_NEWNET) }, 0);
            nested = true;
        }
        assert_eq!(fields[1] == "nested", nested);
        let peer = fields[3].parse().unwrap();
        let alternate = fields[4].parse().unwrap();
        let passed = panic::catch_unwind(|| case(fields[2], peer, alternate)).is_ok();
        println!(
            "DGRAM_CASE {} {}",
            fields[0],
            if passed { "PASS" } else { "FAIL" }
        );
        failed += usize::from(!passed);
    }
    if failed != 0 {
        std::process::exit(1);
    }
    println!("DGRAM_CONNECT_PASS");
}
