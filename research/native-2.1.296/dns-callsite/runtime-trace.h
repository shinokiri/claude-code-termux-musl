/* Research-only call-site observer. No query names, payloads or credentials. */
#include <stdatomic.h>
#include <fcntl.h>
static _Atomic unsigned native_trace_count;
static __attribute__((noinline)) void native_trace_record(const char *api, void *caller) {
    int saved_errno = errno;
    const char *enabled = getenv("NATIVE_DNS_TRACE_FIXTURE");
    const char *output = getenv("NATIVE_DNS_TRACE_FILE");
    const char *dns = getenv("CLAUDE_TERMUX_DNS_SOCKET");
    if (!enabled || strcmp(enabled, "isolated-dns-callsite-v1") || !output ||
        !dns || !strstr(dns, "/claude-dns-") ||
        atomic_fetch_add(&native_trace_count, 1) >= 4096) {
        errno = saved_errno;
        return;
    }
    Dl_info info = {0};
    int found = dladdr(caller, &info);
    const char *base = info.dli_fname ? strrchr(info.dli_fname, '/') : NULL;
    int main_binary = found && base && !strcmp(base + 1, "claude");
    char line[256];
    int size = snprintf(line, sizeof(line),
        "{\"api\":\"%s\",\"main_binary\":%s,\"return_address\":\"0x%llx\",\"load_base\":\"0x%llx\"}\n",
        api, main_binary ? "true" : "false", (unsigned long long)(uintptr_t)caller,
        (unsigned long long)(uintptr_t)info.dli_fbase);
    if (size > 0 && (size_t)size < sizeof(line)) {
        int fd = open(output, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
        if (fd >= 0) { (void)write(fd, line, (size_t)size); (void)close(fd); }
    }
    errno = saved_errno;
}
