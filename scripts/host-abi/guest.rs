use std::{fs, net::TcpListener};
fn main() {
    fs::write("/guest-ran", "yes").unwrap();
    let config = fs::read_to_string("/mode-config").unwrap();
    let mut fields = config.split_whitespace();
    let host = fields.next().unwrap() == "host";
    for address in fields {
        let result = TcpListener::bind(address);
        if host {
            assert_eq!(result.unwrap_err().raw_os_error(), Some(98), "host must reserve native ports");
        } else {
            assert!(result.is_ok(), "legacy bind must remain guest-local: {result:?}");
        }
    }
    println!("CMDLINE={}", fs::read_to_string("/proc/cmdline").unwrap().trim());
    println!("HOST_ABI_VM_PASS mode={}", if host {"host"} else {"legacy"});
}
