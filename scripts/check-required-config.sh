#!/bin/sh
# Guard against slimming cuts silently dropping a kernel feature smolvm relies on.
#
# The libkrunfw arch configs are periodically trimmed to shrink the guest kernel.
# A trim that drops a required symbol still BUILDS fine, so the per-arch build CI
# can't catch it — that is exactly how the x86_64 virtio-GPU/DRM stack got removed
# while aarch64 kept it, breaking `smolvm --gpu` (`/dev/dri` absent) on x86_64.
#
# This check runs on every push/PR (no kernel build, no GPU hardware) and fails
# loudly if any REQUIRED symbol is missing from any guest arch config. Add a line
# here whenever a guest feature depends on a specific CONFIG symbol.
set -u

# Guest arch configs that ship to users. (SEV/TDX/riscv variants are derived or
# experimental and intentionally excluded.)
#
# The Windows config is maintained by hand and has no build job in CI, so it is
# the one most likely to drift: it silently lacked the entire netfilter menu
# while the other two carried it, which is the only reason this list covers it.
CONFIGS='config-libkrunfw_x86_64 config-libkrunfw_aarch64 config-libkrunfw-windows_x86_64'

# "EXACT_CONFIG_LINE|why it is required" — one per line.
#
# A symbol whose dependencies are unmet is dropped by `make olddefconfig` without
# a warning, so listing a leaf option is not enough — the framework symbol it
# hangs off has to be required here too, or the leaf vanishes at build time.
REQUIRED='CONFIG_DRM=y|virtio-GPU DRM core; without it /dev/dri is absent and --gpu is broken
CONFIG_DRM_VIRTIO_GPU=y|virtio-GPU driver; provides card0/renderD128 for --gpu
CONFIG_NETFILTER=y|netfilter core; gates the whole menu, so its absence drops every option below
CONFIG_NF_TABLES=y|nftables; the backend iptables-nft and kube-proxy write rules through
CONFIG_NETFILTER_XTABLES=y|xtables match/target infrastructure used by iptables-legacy
CONFIG_IP_NF_IPTABLES=y|iptables IPv4 tables
CONFIG_IP6_NF_IPTABLES=y|iptables IPv6 tables
CONFIG_NF_CONNTRACK=y|connection tracking; NAT and stateful matches depend on it
CONFIG_NF_NAT=y|NAT; translates service virtual IPs to endpoint addresses
CONFIG_NFT_NUMGEN=y|numgen expression; kube-proxy spreads traffic across endpoints with it
CONFIG_NFT_FIB_INET=y|fib expression; kube-proxy classifies local vs forwarded traffic with it
CONFIG_VXLAN=y|VXLAN overlay; pod networking backends tunnel over it'

fail=0
for cfg in $CONFIGS; do
	if [ ! -f "$cfg" ]; then
		echo "  FAIL  $cfg: file not found"
		fail=1
		continue
	fi
	# IFS split on newline only so reasons may contain spaces.
	OLDIFS=$IFS
	IFS='
'
	for entry in $REQUIRED; do
		IFS=$OLDIFS
		sym=${entry%%|*}
		reason=${entry#*|}
		if grep -qxF "$sym" "$cfg"; then
			echo "  ok    $cfg: $sym"
		else
			echo "  FAIL  $cfg: missing '$sym' — $reason"
			fail=1
		fi
		IFS='
'
	done
	IFS=$OLDIFS
done

if [ "$fail" -ne 0 ]; then
	echo "Required guest kernel config symbols are missing (see above)." >&2
	exit 1
fi
echo "All required guest kernel config symbols present."
