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
CONFIGS='config-libkrunfw_x86_64 config-libkrunfw_aarch64'

# "EXACT_CONFIG_LINE|why it is required" — one per line.
REQUIRED='CONFIG_DRM=y|virtio-GPU DRM core; without it /dev/dri is absent and --gpu is broken
CONFIG_DRM_VIRTIO_GPU=y|virtio-GPU driver; provides card0/renderD128 for --gpu'

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
