/* SPDX-License-Identifier: MIT
 * Resolve musl getaddrinfo calls through Android's existing DNS service.
 * No listener, helper process, HTTP handling or TLS handling.
 * Protocol reference: AOSP bionic libc/dns/net/getaddrinfo.c.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netdb.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

typedef int (*gai_fn)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
typedef void (*free_fn)(struct addrinfo *);
static gai_fn original_gai;
static free_fn original_free;
static pthread_once_t init_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t owned_lock = PTHREAD_MUTEX_INITIALIZER;

struct owned_info {
    struct addrinfo ai;
    struct sockaddr_storage address;
    struct owned_info *owned_next;
};
static struct owned_info *owned;
int termux_native_dns = 1;
static void before_fork(void) { pthread_mutex_lock(&owned_lock); }
static void after_fork(void) { pthread_mutex_unlock(&owned_lock); }

/* ELF DT_NEEDED loads this into every Claude process. Tool children receive
 * their own ABI's execution library at exec, never this musl library. */
__attribute__((constructor)) static void clear_loader_environment(void) {
    unsetenv("LD_PRELOAD");
    const char *mode = getenv("CLAUDE_TERMUX_DNS_MODE");
    if (mode && !strcmp(mode, "http-proxy")) termux_native_dns = 0;
    pthread_atfork(before_fork, after_fork, after_fork);
}

static void initialize(void) {
    *(void **)&original_gai = dlsym(RTLD_NEXT, "getaddrinfo");
    *(void **)&original_free = dlsym(RTLD_NEXT, "freeaddrinfo");
}

void freeaddrinfo(struct addrinfo *result) {
    pthread_once(&init_once, initialize);
    while (result) {
        pthread_mutex_lock(&owned_lock);
        struct owned_info **link = &owned;
        while (*link && &(*link)->ai != result) link = &(*link)->owned_next;
        struct owned_info *item = *link;
        if (item) *link = item->owned_next;
        pthread_mutex_unlock(&owned_lock);
        if (!item) {
            if (original_free) original_free(result);
            return;
        }
        result = item->ai.ai_next;
        free(item->ai.ai_canonname);
        free(item);
    }
}

static int receive_all(int fd, void *destination, size_t size) {
    unsigned char *out = destination;
    while (size) {
        ssize_t n = recv(fd, out, size, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        size -= (size_t)n;
        out += n;
    }
    return 0;
}

static int read_u32(int fd, uint32_t *out) {
    uint32_t value;
    if (receive_all(fd, &value, sizeof(value))) return -1;
    *out = ntohl(value);
    return 0;
}

static int send_all(int fd, const char *data, size_t size) {
    while (size) {
        ssize_t n = send(fd, data, size, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        size -= (size_t)n;
        data += n;
    }
    return 0;
}

static int android_flags(int flags) {
    int value = flags & (AI_PASSIVE | AI_CANONNAME | AI_NUMERICHOST);
    if (flags & AI_NUMERICSERV) value |= 0x008;
    if (flags & AI_ALL) value |= 0x100;
    if (flags & AI_ADDRCONFIG) value |= 0x400;
    if (flags & AI_V4MAPPED) value |= 0x800;
    return value;
}

static int musl_error(int android_error) {
    static const int mapping[] = {0, EAI_ADDRFAMILY, EAI_AGAIN, EAI_BADFLAGS,
        EAI_FAIL, EAI_FAMILY, EAI_MEMORY, EAI_NONAME, EAI_NONAME, EAI_SERVICE,
        EAI_SOCKTYPE, EAI_SYSTEM, EAI_BADFLAGS, EAI_SERVICE, EAI_OVERFLOW};
    return android_error > 0 && (size_t)android_error < sizeof(mapping) / sizeof(*mapping)
        ? mapping[android_error] : EAI_FAIL;
}

static int valid_token(const char *value) {
    if (!value || !*value || strcmp(value, "^") == 0) return 0;
    for (const unsigned char *p = (const unsigned char *)value; *p; p++)
        if (*p <= 32 || *p == 127 || *p == '\\' || *p == '"' || *p == '\'') return 0;
    return strlen(value) <= 1024;
}

int getaddrinfo(const char *host, const char *service, const struct addrinfo *hints,
                struct addrinfo **result) {
    pthread_once(&init_once, initialize);
    if (!result || !original_gai || !original_free) return EAI_SYSTEM;
    if (!termux_native_dns) return original_gai(host, service, hints, result);
    *result = NULL;
    struct addrinfo numeric = {0};
    if (hints) numeric = *hints;
    numeric.ai_flags |= AI_NUMERICHOST;
    int error = original_gai(host, service, &numeric, result);
    if (!error) {
        for (struct addrinfo *ai = *result; ai; ai = ai->ai_next)
            ai->ai_flags = hints ? hints->ai_flags : 0;
        return 0;
    }
    if (error != EAI_NONAME || !host || (hints && (hints->ai_flags & AI_NUMERICHOST))) return error;
    if (!valid_token(host) || (service && !valid_token(service))) return EAI_NONAME;
    if (strcasecmp(host, "localhost") == 0 || strcasecmp(host, "localhost.") == 0)
        return original_gai(host, service, hints, result);

    struct sockaddr_un endpoint = {.sun_family = AF_UNIX};
    const char *path = getenv("CLAUDE_TERMUX_DNS_SOCKET");
    if (!path || !*path) path = "/dev/socket/dnsproxyd";
    if (strlen(path) >= sizeof(endpoint.sun_path)) return EAI_SYSTEM;
    strcpy(endpoint.sun_path, path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return EAI_SYSTEM;
    struct timeval timeout = {.tv_sec = 30};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    error = EAI_SYSTEM;
    struct addrinfo *head = NULL, **tail = &head;
    if (connect(fd, (struct sockaddr *)&endpoint, sizeof(endpoint))) goto done;
    char command[2300];
    int size = snprintf(command, sizeof(command), "getaddrinfo %s %s %d %d %d %d 0",
        host, service ? service : "^", hints ? android_flags(hints->ai_flags) : -1,
        hints ? hints->ai_family : -1, hints ? hints->ai_socktype : -1,
        hints ? hints->ai_protocol : -1);
    if (size < 0 || (size_t)size >= sizeof(command) || send_all(fd, command, (size_t)size + 1)) goto done;
    char status[4];
    if (receive_all(fd, status, sizeof(status))) goto done;
    if (memcmp(status, "222\0", 4)) {
        int32_t remote_error;
        error = receive_all(fd, &remote_error, sizeof(remote_error)) ? EAI_FAIL : musl_error(remote_error);
        goto done;
    }
    for (unsigned count = 0; count < 256; count++) {
        uint32_t more, fields[5], canonical_length;
        if (read_u32(fd, &more)) goto done;
        if (!more) { error = head ? 0 : EAI_NONAME; break; }
        if (more != 1) { error = EAI_FAIL; goto done; }
        for (unsigned i = 0; i < 5; i++) if (read_u32(fd, fields + i)) goto done;
        if ((fields[1] != AF_INET && fields[1] != AF_INET6) ||
            fields[4] != (fields[1] == AF_INET ? sizeof(struct sockaddr_in) : sizeof(struct sockaddr_in6))) {
            error = EAI_FAIL; goto done;
        }
        struct owned_info *item = calloc(1, sizeof(*item));
        if (!item) { error = EAI_MEMORY; goto done; }
        *tail = &item->ai;
        tail = &item->ai.ai_next;
        item->ai.ai_flags = hints ? hints->ai_flags : 0;
        item->ai.ai_family = (int)fields[1];
        item->ai.ai_socktype = (int)fields[2];
        item->ai.ai_protocol = (int)fields[3];
        item->ai.ai_addrlen = fields[4];
        item->ai.ai_addr = (struct sockaddr *)&item->address;
        if (receive_all(fd, &item->address, fields[4]) || read_u32(fd, &canonical_length)) goto done;
        if (item->address.ss_family != fields[1] || canonical_length > 1025) { error = EAI_FAIL; goto done; }
        if (canonical_length) {
            item->ai.ai_canonname = malloc(canonical_length);
            if (!item->ai.ai_canonname) { error = EAI_MEMORY; goto done; }
            if (receive_all(fd, item->ai.ai_canonname, canonical_length)) goto done;
            if (item->ai.ai_canonname[canonical_length - 1]) { error = EAI_FAIL; goto done; }
        }
    }
done:
    {
        int saved_errno = errno;
        close(fd);
        errno = saved_errno;
    }
    if (error) {
        while (head) {
            struct addrinfo *next = head->ai_next;
            free(head->ai_canonname);
            free(head);
            head = next;
        }
        return error;
    }
    pthread_mutex_lock(&owned_lock);
    for (struct addrinfo *ai = head; ai; ai = ai->ai_next) {
        struct owned_info *item = (struct owned_info *)ai;
        item->owned_next = owned;
        owned = item;
    }
    pthread_mutex_unlock(&owned_lock);
    *result = head;
    return 0;
}
