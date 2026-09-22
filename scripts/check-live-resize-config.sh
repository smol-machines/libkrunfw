#!/bin/sh
# Live resize prerequisites; also run against the resolved guest .config.
set -eu
config=${1:-config-libkrunfw_x86_64}
failed=0
symbols="HOTPLUG_CPU MEMORY_HOTPLUG MEMORY_HOTREMOVE SPARSEMEM_VMEMMAP MHP_MEMMAP_ON_MEMORY VIRTIO_MEM"
case ${2:-x86_64} in
    x86_64) symbols="$symbols ACPI ACPI_REDUCED_HARDWARE_ONLY ACPI_PROCESSOR ACPI_HOTPLUG_CPU" ;;
    aarch64) symbols="$symbols ARM_PSCI_FW ARM64_4K_PAGES MEMORY_ISOLATION CONTIG_ALLOC EXCLUSIVE_SYSTEM_RAM VIRTIO_MMIO" ;;
    *) echo "unsupported guest architecture: $2" >&2; exit 1 ;;
esac
for symbol in $symbols; do
    if ! grep -qxF "CONFIG_${symbol}=y" "$config"; then
        echo "$config: missing CONFIG_${symbol}=y for live CPU/RAM growth" >&2
        failed=1
    fi
done
if ! grep -qxF '# CONFIG_MEMORY_HOTPLUG_DEFAULT_ONLINE is not set' "$config"; then
    echo "$config: memory onlining must remain agent-controlled" >&2
    failed=1
fi
exit "$failed"
