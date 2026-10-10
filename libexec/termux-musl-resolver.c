/* SPDX-License-Identifier: MIT
 * Preserve c-ares' DNS APIs, parser, timeouts and per-Resolver configuration.
 * Its default resolver is an in-process socketpair backed by Android resnsend.
 * There is no IP listener, helper process, public DNS server or HTTP/TLS proxy.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#define DNS_SENTINEL 0x7f000035u /* 127.0.0.53; intercepted before connecting */
#define DNS_PORT 53
#define MAX_DNS 65535u
#define MAX_COMMAND 4096u /* Android FrameworkListener's command bound */
#define MAX_PENDING 128u
#define QUERY_TIMEOUT_MS 30000

static int (*next_connect)(int, const struct sockaddr *, socklen_t);
static int (*next_close)(int);
static int (*next_getsockname)(int, struct sockaddr *, socklen_t *);
static int (*next_getpeername)(int, struct sockaddr *, socklen_t *);
static FILE *(*next_fopen)(const char *, const char *);
static ssize_t (*next_sendto)(int, const void *, size_t, int, const struct sockaddr *, socklen_t);
static ssize_t (*next_recvfrom)(int, void *, size_t, int, struct sockaddr *, socklen_t *);
static ssize_t (*next_sendmsg)(int, const struct msghdr *, int);
static ssize_t (*next_recvmsg)(int, struct msghdr *, int);
static pthread_once_t symbols_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t bridges_lock = PTHREAD_MUTEX_INITIALIZER;
extern int termux_native_dns;

struct bridge {
    int client, peer, type;
    unsigned refs;
    struct bridge *next;
};
static struct bridge *bridges;
static void before_fork(void) { pthread_mutex_lock(&bridges_lock); }
static void after_fork(void) { pthread_mutex_unlock(&bridges_lock); }

__attribute__((constructor)) static void resolver_fork_locks(void) {
    pthread_atfork(before_fork, after_fork, after_fork);
}

static void symbols_init(void) {
#define LOAD(variable, name) *(void **)&variable = dlsym(RTLD_NEXT, name)
    LOAD(next_connect, "connect"); LOAD(next_close, "close");
    LOAD(next_getsockname, "getsockname"); LOAD(next_getpeername, "getpeername");
    LOAD(next_fopen, "fopen"); LOAD(next_sendto, "sendto"); LOAD(next_recvfrom, "recvfrom");
    LOAD(next_sendmsg, "sendmsg"); LOAD(next_recvmsg, "recvmsg");
#undef LOAD
}

static int bridge_type(int fd) {
    int type = 0;
    pthread_mutex_lock(&bridges_lock);
    for (struct bridge *b = bridges; b; b = b->next)
        if (b->client == fd) { type = b->type; break; }
    pthread_mutex_unlock(&bridges_lock);
    return type;
}

static void bridge_release(struct bridge *b) {
    pthread_mutex_lock(&bridges_lock);
    unsigned refs = --b->refs;
    pthread_mutex_unlock(&bridges_lock);
    if (!refs) free(b);
}

FILE *fopen(const char *path, const char *mode) {
    pthread_once(&symbols_once, symbols_init);
    /* c-ares opens this file; musl's hidden resolver calls are handled separately.
     * Explicit Resolver.setServers() still uses its selected IPs and ports. */
    static char config[] = "nameserver 127.0.0.53\n";
    if (termux_native_dns && strcmp(path, "/etc/resolv.conf") == 0 && mode[0] == 'r')
        return fmemopen(config, sizeof(config) - 1, "r");
    return next_fopen(path, mode);
}

static int64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static unsigned u16(const unsigned char *p) { return (unsigned)p[0] * 256 + p[1]; }
static uint32_t u32(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static int skip_name(const unsigned char *p, size_t size, size_t *at) {
    for (unsigned labels = 0; labels < 128 && *at < size; labels++) {
        unsigned n = p[(*at)++];
        if (!n) return 0;
        if ((n & 0xc0) == 0xc0) {
            if (*at >= size) return -1;
            (*at)++; return 0;
        }
        if (n > 63 || n > size - *at) return -1;
        *at += n;
    }
    return -1;
}

/* The raw Android API can return a full TCP answer. Respect the caller's UDP
 * limit and let c-ares perform its normal TCP retry when that answer is larger. */
static unsigned udp_limit(const unsigned char *p, size_t size) {
    if (size < 12) return 512;
    size_t at = 12;
    for (unsigned i = 0; i < u16(p + 4); i++) {
        if (skip_name(p, size, &at) || at + 4 > size) return 512;
        at += 4;
    }
    unsigned records = u16(p + 6) + u16(p + 8) + u16(p + 10);
    for (unsigned i = 0; i < records; i++) {
        if (skip_name(p, size, &at) || at + 10 > size) return 512;
        unsigned type = u16(p + at), cls = u16(p + at + 2), length = u16(p + at + 8);
        if (type == 41) return cls < 512 ? 512 : cls;
        at += 10;
        if (length > size - at) return 512;
        at += length;
    }
    return 512;
}

struct query {
    int fd;
    unsigned char *request, *output;
    size_t request_size, output_size, output_at;
    char command[MAX_COMMAND];
    size_t command_size, command_at;
    unsigned char header[8];
    size_t header_size, answer_size, answer_at;
    int64_t deadline;
    int ready;
    struct query *next;
};

static void query_free(struct query *q) {
    if (q->fd >= 0) next_close(q->fd);
    free(q->request); free(q->output); free(q);
}

static void query_finish(struct query *q, int type) {
    if (q->fd >= 0) { next_close(q->fd); q->fd = -1; }
    q->output[0] = (unsigned char)(q->answer_size >> 8);
    q->output[1] = (unsigned char)q->answer_size;
    q->output_size = q->answer_size + 2;
    q->output_at = type == SOCK_STREAM ? 0 : 2;
    q->ready = 1;
}

static int query_error(struct query *q, int type, int truncated) {
    free(q->output);
    q->output = malloc(q->request_size + 2);
    if (!q->output) return -1;
    memcpy(q->output + 2, q->request, q->request_size);
    unsigned char *p = q->output + 2;
    p[2] = (unsigned char)((p[2] & 1) | 0x80 | (truncated ? 2 : 0));
    p[3] = (unsigned char)(0x80 | (truncated ? 0 : 2)); /* RA / SERVFAIL */
    memset(p + 6, 0, 4); /* no answer or authority; keep the query's OPT record */
    q->answer_size = q->request_size;
    query_finish(q, type);
    return 0;
}

static struct query *query_start(const unsigned char *packet, size_t size, int type) {
    if (size < 12 || size > MAX_DNS) return NULL;
    struct query *q = calloc(1, sizeof(*q));
    if (!q) return NULL;
    q->fd = -1;
    q->request = malloc(size);
    if (!q->request) { query_free(q); return NULL; }
    memcpy(q->request, packet, size);
    q->request_size = size;
    q->deadline = now_ms() + QUERY_TIMEOUT_MS;
    size_t encoded = (size + 2) / 3 * 4;
    const char prefix[] = "resnsend 0 0 ";
    if (encoded + sizeof(prefix) > sizeof(q->command)) goto failure;
    memcpy(q->command, prefix, sizeof(prefix) - 1);
    size_t at = sizeof(prefix) - 1;
    static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    for (size_t i = 0; i < size; i += 3) {
        unsigned value = (unsigned)packet[i] << 16;
        if (i + 1 < size) value |= (unsigned)packet[i + 1] << 8;
        if (i + 2 < size) value |= packet[i + 2];
        q->command[at++] = alphabet[(value >> 18) & 63];
        q->command[at++] = alphabet[(value >> 12) & 63];
        q->command[at++] = i + 1 < size ? alphabet[(value >> 6) & 63] : '=';
        q->command[at++] = i + 2 < size ? alphabet[value & 63] : '=';
    }
    q->command[at++] = 0;
    q->command_size = at;
    struct sockaddr_un endpoint = {.sun_family = AF_UNIX};
    const char *path = getenv("CLAUDE_TERMUX_DNS_SOCKET");
    if (!path || !*path) path = "/dev/socket/dnsproxyd";
    if (strlen(path) >= sizeof(endpoint.sun_path)) goto failure;
    strcpy(endpoint.sun_path, path);
    q->fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (q->fd < 0) goto failure;
    if (next_connect(q->fd, (struct sockaddr *)&endpoint, sizeof(endpoint)) && errno != EINPROGRESS)
        goto failure;
    return q;
failure:
    if (query_error(q, type, 0)) { query_free(q); return NULL; }
    return q;
}

/* Return 1 to silently discard a network timeout (c-ares owns its timers),
 * -1 for malformed IPC, and 0 for progress or a completed DNS response. */
static int query_io(struct query *q, int type) {
    while (q->command_at < q->command_size) {
        ssize_t n = send(q->fd, q->command + q->command_at, q->command_size - q->command_at, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
        if (n <= 0) return -1;
        q->command_at += (size_t)n;
        if (q->command_at == q->command_size) shutdown(q->fd, SHUT_WR);
    }
    for (;;) {
        void *destination;
        size_t wanted;
        if (q->header_size < 8) {
            destination = q->header + q->header_size;
            wanted = (q->header_size < 4 ? 4 : 8) - q->header_size;
        } else {
            destination = q->output + 2 + q->answer_at;
            wanted = q->answer_size - q->answer_at;
        }
        ssize_t n = recv(q->fd, destination, wanted, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return 0;
        if (n <= 0) return -1;
        if (q->header_size < 8) {
            q->header_size += (size_t)n;
            if (q->header_size == 4 && (int32_t)u32(q->header) < 0)
                return (int32_t)u32(q->header) == -ETIMEDOUT ? 1 : -1;
            if (q->header_size == 8) {
                q->answer_size = u32(q->header + 4);
                if (u32(q->header) > 15 || q->answer_size < 12 || q->answer_size > MAX_DNS) return -1;
                q->output = malloc(q->answer_size + 2);
                if (!q->output) return -1;
            }
        } else {
            q->answer_at += (size_t)n;
            if (q->answer_at == q->answer_size) {
                if (memcmp(q->output + 2, q->request, 2) || !(q->output[4] & 0x80)) return -1;
                if (type == SOCK_DGRAM && q->answer_size > udp_limit(q->request, q->request_size))
                    return query_error(q, type, 1);
                query_finish(q, type);
                return 0;
            }
        }
    }
}

static void *bridge_worker(void *argument) {
    struct bridge *b = argument;
    struct query *queries = NULL;
    struct query *stream_output = NULL;
    unsigned count = 0;
    unsigned char *input = malloc(MAX_DNS + 2);
    size_t input_size = 0;
    if (!input) goto done;
    for (;;) {
        struct pollfd polls[MAX_PENDING + 1];
        struct query *mapping[MAX_PENDING + 1];
        nfds_t used = 1;
        polls[0] = (struct pollfd){.fd = b->peer, .events = count < MAX_PENDING ? POLLIN : 0};
        int timeout = QUERY_TIMEOUT_MS;
        int64_t now = now_ms();
        if (count < MAX_PENDING && input_size &&
            (b->type != SOCK_STREAM || (input_size >= 2 && input_size >= 2 + u16(input))))
            timeout = 0;
        for (struct query *q = queries; q; q = q->next) {
            if (q->ready) polls[0].events |= POLLOUT;
            else {
                polls[used] = (struct pollfd){.fd = q->fd, .events = q->command_at < q->command_size ? POLLOUT : POLLIN};
                mapping[used++] = q;
            }
            int64_t remaining = q->deadline - now;
            if (remaining < timeout) timeout = remaining > 0 ? (int)remaining : 0;
        }
        int ready = poll(polls, used, timeout);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0 || (polls[0].revents & (POLLHUP | POLLERR | POLLNVAL))) break;
        for (nfds_t i = 1; i < used; i++) {
            struct query *q = mapping[i];
            if (!polls[i].revents) continue;
            int status = query_io(q, b->type);
            if (status > 0) q->deadline = 0;
            if (status < 0 && query_error(q, b->type, 0)) q->deadline = 0;
        }
        if (polls[0].revents & POLLIN) {
            ssize_t n = recv(b->peer, input + input_size, MAX_DNS + 2 - input_size, 0);
            if (n == 0) break;
            if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
            if (n > 0) input_size += (size_t)n;
        }
        while (input_size && count < MAX_PENDING) {
            size_t prefix = b->type == SOCK_STREAM ? 2 : 0;
            if (input_size < prefix) break;
            size_t size = prefix ? u16(input) : input_size;
            if (size + prefix > input_size) break;
            struct query *q = query_start(input + prefix, size, b->type);
            if (q) { q->next = queries; queries = q; count++; }
            input_size -= size + prefix;
            memmove(input, input + size + prefix, input_size);
        }
        now = now_ms();
        struct query **link = &queries;
        while (*link) {
            struct query *q = *link;
            int remove = q->deadline <= now;
            /* Never interleave TCP frames or drop a partially written frame. */
            if (remove && b->type == SOCK_STREAM && q->ready && q->output_at) goto done;
            if (!remove && q->ready) {
                if (b->type == SOCK_STREAM) {
                    if (stream_output && stream_output != q) { link = &q->next; continue; }
                    stream_output = q;
                }
                size_t wanted = q->output_size - q->output_at;
                ssize_t n = send(b->peer, q->output + q->output_at, wanted, MSG_NOSIGNAL);
                if (n > 0) q->output_at += (size_t)n;
                else if (n < 0 && errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) goto done;
                remove = q->output_at == q->output_size;
                if (!remove && b->type == SOCK_STREAM) break;
            }
            if (remove) {
                if (stream_output == q) stream_output = NULL;
                *link = q->next; query_free(q); count--;
            }
            else link = &q->next;
        }
    }
done:
    while (queries) { struct query *next = queries->next; query_free(queries); queries = next; }
    free(input);
    next_close(b->peer);
    bridge_release(b);
    return NULL;
}

int connect(int fd, const struct sockaddr *address, socklen_t length) {
    pthread_once(&symbols_once, symbols_init);
    if (!termux_native_dns || !address || length < sizeof(struct sockaddr_in) || address->sa_family != AF_INET)
        return next_connect(fd, address, length);
    const struct sockaddr_in *in = (const struct sockaddr_in *)address;
    if (in->sin_addr.s_addr != htonl(DNS_SENTINEL) || in->sin_port != htons(DNS_PORT))
        return next_connect(fd, address, length);
    int type = 0;
    socklen_t type_size = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_size)) return -1;
    if (type != SOCK_DGRAM && type != SOCK_STREAM) return next_connect(fd, address, length);
    if (bridge_type(fd)) return 0;
    struct bridge *b = calloc(1, sizeof(*b));
    if (!b) { errno = ENOMEM; return -1; }
    int pair[2];
    if (socketpair(AF_UNIX, (type == SOCK_DGRAM ? SOCK_SEQPACKET : SOCK_STREAM) | SOCK_CLOEXEC, 0, pair)) {
        free(b); return -1;
    }
    int flags = fcntl(fd, F_GETFL), fd_flags = fcntl(fd, F_GETFD);
    if (flags < 0 || fd_flags < 0 || dup3(pair[0], fd, fd_flags & FD_CLOEXEC ? O_CLOEXEC : 0) < 0) {
        int saved = errno; next_close(pair[0]); next_close(pair[1]); free(b); errno = saved; return -1;
    }
    next_close(pair[0]);
    if (fcntl(fd, F_SETFL, flags) < 0 || fcntl(pair[1], F_SETFL, O_NONBLOCK) < 0) {
        int saved = errno; next_close(pair[1]); free(b); errno = saved; return -1;
    }
    b->client = fd; b->peer = pair[1]; b->type = type; b->refs = 2;
    pthread_mutex_lock(&bridges_lock);
    b->next = bridges; bridges = b;
    pthread_mutex_unlock(&bridges_lock);
    pthread_t worker;
    int error = pthread_create(&worker, NULL, bridge_worker, b);
    if (error) {
        pthread_mutex_lock(&bridges_lock);
        struct bridge **link = &bridges;
        while (*link && *link != b) link = &(*link)->next;
        if (*link) *link = b->next;
        pthread_mutex_unlock(&bridges_lock);
        next_close(b->peer); free(b); errno = error; return -1;
    }
    pthread_detach(worker);
    return 0;
}

int close(int fd) {
    pthread_once(&symbols_once, symbols_init);
    pthread_mutex_lock(&bridges_lock);
    struct bridge **link = &bridges;
    while (*link && (*link)->client != fd) link = &(*link)->next;
    struct bridge *b = *link;
    if (b) *link = b->next;
    pthread_mutex_unlock(&bridges_lock);
    int result = next_close(fd), saved = errno;
    if (b) bridge_release(b);
    errno = saved;
    return result;
}

static int socket_name(struct sockaddr *out, socklen_t *length, int peer) {
    if (!out || !length) { errno = EFAULT; return -1; }
    struct sockaddr_in value = {.sin_family = AF_INET, .sin_port = htons(peer ? DNS_PORT : 0),
        .sin_addr.s_addr = htonl(peer ? DNS_SENTINEL : 0x7f000001)};
    memcpy(out, &value, *length < sizeof(value) ? *length : sizeof(value));
    *length = sizeof(value);
    return 0;
}

int getsockname(int fd, struct sockaddr *out, socklen_t *length) {
    pthread_once(&symbols_once, symbols_init);
    return bridge_type(fd) ? socket_name(out, length, 0) : next_getsockname(fd, out, length);
}
int getpeername(int fd, struct sockaddr *out, socklen_t *length) {
    pthread_once(&symbols_once, symbols_init);
    return bridge_type(fd) ? socket_name(out, length, 1) : next_getpeername(fd, out, length);
}
ssize_t sendto(int fd, const void *data, size_t size, int flags, const struct sockaddr *address, socklen_t length) {
    pthread_once(&symbols_once, symbols_init);
    int ours = bridge_type(fd);
    return next_sendto(fd, data, size, flags, ours ? NULL : address, ours ? 0 : length);
}
ssize_t recvfrom(int fd, void *data, size_t size, int flags, struct sockaddr *address, socklen_t *length) {
    pthread_once(&symbols_once, symbols_init);
    if (!bridge_type(fd)) return next_recvfrom(fd, data, size, flags, address, length);
    ssize_t result = next_recvfrom(fd, data, size, flags, NULL, NULL);
    if (result >= 0 && address && socket_name(address, length, 1)) return -1;
    return result;
}
ssize_t sendmsg(int fd, const struct msghdr *message, int flags) {
    pthread_once(&symbols_once, symbols_init);
    if (!bridge_type(fd) || !message || !message->msg_name) return next_sendmsg(fd, message, flags);
    struct msghdr adjusted = *message;
    adjusted.msg_name = NULL; adjusted.msg_namelen = 0;
    return next_sendmsg(fd, &adjusted, flags);
}
ssize_t recvmsg(int fd, struct msghdr *message, int flags) {
    pthread_once(&symbols_once, symbols_init);
    if (!bridge_type(fd) || !message) return next_recvmsg(fd, message, flags);
    struct msghdr adjusted = *message;
    adjusted.msg_name = NULL; adjusted.msg_namelen = 0;
    ssize_t result = next_recvmsg(fd, &adjusted, flags);
    message->msg_flags = adjusted.msg_flags; message->msg_controllen = adjusted.msg_controllen;
    if (result >= 0 && message->msg_name && socket_name(message->msg_name, &message->msg_namelen, 1)) return -1;
    return result;
}
