#!/bin/sh
# Linux x86_64 live resize prerequisites; also run against resolved .config.
set -eu
config=${1:-config-libkrunfw_x86_64}
failed=0
for symbol in ACPI ACPI_REDUCED_HARDWARE_ONLY ACPI_PROCESSOR ACPI_HOTPLUG_CPU \
    HOTPLUG_CPU MEMORY_HOTPLUG MEMORY_HOTREMOVE SPARSEMEM_VMEMMAP VIRTIO_MEM; do
    if ! grep -qxF "CONFIG_${symbol}=y" "$config"; then
        echo "$config: missing CONFIG_${symbol}=y for live CPU/RAM growth" >&2
        failed=1
    fi
done
exit "$failed"
