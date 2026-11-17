#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
char *krunfw_get_kernel(uint64_t *load, uint64_t *entry, size_t *size) {
    FILE *f = fopen(getenv("SMOL_ABI_RAW"), "rb");
    if (!f) abort();
    fseek(f, 0, SEEK_END); *size = (size_t)ftell(f); rewind(f);
    void *data = NULL;
    if (posix_memalign(&data, 65536, *size)) abort();
    if (fread(data, 1, *size, f) != *size) abort();
    fclose(f);
    *load = strtoull(getenv("SMOL_ABI_LOAD"), NULL, 0);
    *entry = strtoull(getenv("SMOL_ABI_ENTRY"), NULL, 0);
    return data;
}
