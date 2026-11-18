#define _GNU_SOURCE
#include "libkrun.h"
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
extern int32_t krun_set_log_level(uint32_t);
extern int32_t krun_set_snapshot(uint32_t, const char *);
#define LOAD(name)                                                             \
  __typeof__(name) *p_##name = dlsym(lib, #name);                              \
  if (!p_##name) {                                                             \
    fprintf(stderr, "missing %s\n", #name);                                    \
    return 2;                                                                  \
  }
#define CALL(name, ...)                                                        \
  do {                                                                         \
    int ret = p_##name(__VA_ARGS__);                                           \
    if (ret < 0) {                                                             \
      fprintf(stderr, "%s: %d\n", #name, ret);                                 \
      return 2;                                                                \
    }                                                                          \
  } while (0)
int main(int argc, char **argv) {
  if (argc != 4)
    return 2;
  setenv("SMOLVM_ALLOW_HOST_LOOPBACK", "1", 1);
  void *lib = dlopen(argv[3], RTLD_NOW | RTLD_LOCAL);
  if (!lib) {
    fprintf(stderr, "%s\n", dlerror());
    return 2;
  }
  LOAD(krun_set_stream_intercept);
  LOAD(krun_set_dns_forwarder);
  LOAD(krun_set_log_level);
  LOAD(krun_create_ctx);
  LOAD(krun_set_vm_config);
  LOAD(krun_add_vsock);
  LOAD(krun_set_kernel);
  LOAD(krun_add_virtio_console_default);
  LOAD(krun_add_virtiofs3);
  LOAD(krun_set_workdir);
  LOAD(krun_set_exec);
  LOAD(krun_start_enter);
  LOAD(krun_set_control_socket);
  LOAD(krun_set_snapshot);
  // Darwin library search paths can override even an absolute dlopen path.
  // Verify the loaded object before booting, not merely the requested file.
  Dl_info loaded;
  struct stat requested_stat, loaded_stat;
  if (!dladdr((void *)p_krun_create_ctx, &loaded) ||
      stat(argv[3], &requested_stat) || stat(loaded.dli_fname, &loaded_stat) ||
      requested_stat.st_dev != loaded_stat.st_dev ||
      requested_stat.st_ino != loaded_stat.st_ino) {
    fprintf(stderr, "runtime library mismatch: requested %s\n", argv[3]);
    return 2;
  }
  printf("SMOL_RUNTIME_LIBRARY %s\n", loaded.dli_fname);
  fflush(stdout);
  CALL(krun_set_log_level, 4);
  int ctx = p_krun_create_ctx();
  if (ctx < 0)
    return 2;
  CALL(krun_set_vm_config, ctx, 1, 512);
  int legacy = getenv("SMOL_HOST_MODE") &&
               strcmp(getenv("SMOL_HOST_MODE"), "legacy") == 0;
  CALL(krun_add_vsock, ctx, legacy ? 1 : 5);
  CALL(krun_set_dns_forwarder, ctx, getenv("SMOL_DNS_FORWARDER"));
  if (getenv("SMOL_TCP_INTERCEPT"))
    CALL(krun_set_stream_intercept, ctx, getenv("SMOL_TCP_INTERCEPT"),
         "0000000000000000000000000000000000000000000000000000000000000000",
         18443);
  CALL(krun_set_kernel, ctx, argv[2],
#ifdef __x86_64__
       KRUN_KERNEL_FORMAT_ELF,
#else
       KRUN_KERNEL_FORMAT_RAW,
#endif
       NULL,
       legacy ? "reboot=k panic=-1 console=hvc0 rootfstype=virtiofs "
                "root=/dev/root rw init=/init.krun tsi_host_network=0"
              : "reboot=k panic=-1 console=hvc0 rootfstype=virtiofs "
                "root=/dev/root rw init=/init.krun tsi_host_network=1");
  CALL(krun_add_virtio_console_default, ctx, 0, 1, 2);
  CALL(krun_add_virtiofs3, ctx, "/dev/root", argv[1], 0, false);
  CALL(krun_set_workdir, ctx, "/");
  const char *args[] = {NULL};
  const char *env[] = {"RUST_BACKTRACE=1", NULL};
  CALL(krun_set_exec, ctx, "/probe", args, env);
  CALL(krun_set_control_socket, ctx, getenv("SMOL_TEST_CONTROL"));
  if (getenv("SMOL_TEST_SNAPSHOT"))
    CALL(krun_set_snapshot, ctx, getenv("SMOL_TEST_SNAPSHOT"));
  CALL(krun_start_enter, ctx);
  return 2;
}
