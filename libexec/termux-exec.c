/* SPDX-License-Identifier: MIT
 * Native execution boundary shared by the musl and Bionic builds.
 * All allocation/symbol lookup happens before a possible vfork child calls us.
 * Runtime ELF metadata, not a list of Claude command names, selects the ABI.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <dirent.h>
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <sys/syscall.h>
#include <unistd.h>

#ifndef TERMUX_PREFIX
#define TERMUX_PREFIX "/data/data/com.termux/files/usr"
#endif
#define TERMUX_BIN TERMUX_PREFIX "/bin/"
#define VECTOR_LIMIT 16384
#define SCRIPT_LIMIT 5
extern char **environ;
typedef int (*exec_fn)(const char *, char *const[], char *const[]);
static exec_fn next_execve;
static dev_t claude_device;
static ino_t claude_inode;
static int claude_identity_known;
static int release_lease_fd = -1;
static dev_t release_lease_device;
static ino_t release_lease_inode;

/* Shared across exec and inherited by descendants. The collector needs an
 * exclusive lock before removing a release. Reacquire after spawn APIs close
 * unrelated descriptors, before crossing exec; no allocation in that path. */
static int lease_guard(void) {
    struct stat info;
    if (release_lease_fd >= 0 && !fstat(release_lease_fd, &info) &&
        info.st_dev == release_lease_device && info.st_ino == release_lease_inode) return -1;
    int fd = (int)syscall(SYS_openat, AT_FDCWD, RELEASE_LEASE, O_RDONLY | O_NOFOLLOW, 0);
    if (fd < 0) return -2;
    if (syscall(SYS_flock, fd, LOCK_SH | LOCK_NB)) {
        int saved = errno;
        syscall(SYS_close, fd);
        errno = saved;
        return -2;
    }
    return fd;
}

static void lease_initialize(void) {
    struct stat lease;
    if (stat(RELEASE_LEASE, &lease)) goto failure;
    DIR *directory = opendir("/proc/self/fd");
    if (!directory) goto failure;
    struct dirent *entry;
    while ((entry = readdir(directory))) {
        char *end;
        long value = strtol(entry->d_name, &end, 10);
        struct stat info;
        if (*end || value < 0 || value > INT_MAX || fstat((int)value, &info)) continue;
        if (info.st_dev == lease.st_dev && info.st_ino == lease.st_ino) {
            if (syscall(SYS_flock, (int)value, LOCK_SH | LOCK_NB)) continue;
            release_lease_fd = (int)value;
            fcntl(release_lease_fd, F_SETFD, 0);
            break;
        }
    }
    closedir(directory);
    if (release_lease_fd < 0) release_lease_fd = lease_guard();
    if (release_lease_fd < 0) goto failure;
    release_lease_device = lease.st_dev;
    release_lease_inode = lease.st_ino;
    return;
failure:
    {
        const char message[] = "claude-termux: release lease unavailable; restart from the claude command\n";
        syscall(SYS_write, STDERR_FILENO, message, sizeof(message) - 1);
        _exit(78);
    }
}
#ifdef __BIONIC__
static int privacy_active;
static int termux_privacy_enabled(void) { return privacy_active; }
#else
extern int termux_privacy_enabled(void);
#endif

static const char *without_our_preload(const char *value) {
    size_t own = strlen(BIONIC_EXEC_LIBRARY);
    if (!strncmp(value, BIONIC_EXEC_LIBRARY, own) && (!value[own] || value[own] == ':' || value[own] == ' ')) {
        value += own;
        while (*value == ':' || *value == ' ') value++;
    }
    return value;
}

static const char *env_value(char *const env[], const char *name) {
    size_t length = strlen(name);
    if (env) for (size_t i = 0; env[i]; i++)
        if (!strncmp(env[i], name, length) && env[i][length] == '=') return env[i] + length + 1;
    return NULL;
}

__attribute__((constructor)) static void exec_initialize(void) {
    lease_initialize();
    *(void **)&next_execve = dlsym(RTLD_NEXT, "execve");
    struct stat binary;
    if (!stat(CLAUDE_BINARY, &binary)) {
        claude_device = binary.st_dev;
        claude_inode = binary.st_ino;
        claude_identity_known = 1;
    }
#ifdef __BIONIC__
    const char *mode = getenv("CLAUDE_TERMUX_PRIVACY");
    privacy_active = mode && !strcmp(mode, "optional-off");
#endif
}

static int is_claude(const char *path) {
    if (!strcmp(path, CLAUDE_BINARY)) return 1;
    if (!claude_identity_known) return 0;
    int saved = errno;
    struct stat binary;
    int same = !stat(path, &binary) && binary.st_dev == claude_device && binary.st_ino == claude_inode;
    errno = saved;
    return same;
}

static int vector_length(char *const vector[], size_t *length) {
    *length = 0;
    if (vector) while (vector[*length]) {
        if (*length == VECTOR_LIMIT) { errno = E2BIG; return -1; }
        ++*length;
    }
    return 0;
}

static const char *map_path(const char *path, char buffer[PATH_MAX]) {
    static const char *const aliases[] = {"/bin/", "/usr/bin/", "/sbin/", "/usr/sbin/"};
    for (size_t i = 0; i < sizeof(aliases) / sizeof(*aliases); i++) {
        size_t length = strlen(aliases[i]);
        if (!strncmp(path, aliases[i], length)) {
            size_t rest = strlen(path + length);
            if (sizeof(TERMUX_BIN) + rest > PATH_MAX) { errno = ENAMETOOLONG; return NULL; }
            memcpy(buffer, TERMUX_BIN, sizeof(TERMUX_BIN) - 1);
            memcpy(buffer + sizeof(TERMUX_BIN) - 1, path + length, rest + 1);
            return buffer;
        }
    }
    return path;
}

/* 0: kernel/Termux handles it, 1: Bionic, 2: musl/other dynamic ELF, 3: script. */
static int inspect(const char *path, char script[256]) {
    int saved = errno;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) return 0;
    struct stat info;
    int kind = 0;
    Elf64_Ehdr header;
    if (fstat(fd, &info) || !S_ISREG(info.st_mode)) goto done;
    ssize_t count = pread(fd, script, 256, 0);
    if (count >= 2 && script[0] == '#' && script[1] == '!') {
        /* Match the kernel's 256-byte shebang limit; never truncate a path. */
        char *newline = memchr(script, '\n', (size_t)count);
        if (newline) *newline = 0;
        else if (count < 256) script[count] = 0;
        else goto done;
        kind = 3;
        goto done;
    }
    if (count < (ssize_t)sizeof(header)) goto done;
    memcpy(&header, script, sizeof(header));
    if (memcmp(header.e_ident, "\177ELF\2\1\1", 7) || header.e_machine != EM_AARCH64 ||
        header.e_phentsize != sizeof(Elf64_Phdr) || header.e_phnum > 256 ||
        header.e_phoff > (uint64_t)info.st_size ||
        header.e_phnum * sizeof(Elf64_Phdr) > (uint64_t)info.st_size - header.e_phoff) goto done;
    for (unsigned i = 0; i < header.e_phnum; i++) {
        Elf64_Phdr ph;
        if (pread(fd, &ph, sizeof(ph), (off_t)(header.e_phoff + i * sizeof(ph))) != sizeof(ph)) break;
        if (ph.p_type != PT_INTERP) continue;
        char interpreter[PATH_MAX];
        if (!ph.p_filesz || ph.p_filesz > sizeof(interpreter) || ph.p_offset > (uint64_t)info.st_size ||
            ph.p_filesz > (uint64_t)info.st_size - ph.p_offset ||
            pread(fd, interpreter, (size_t)ph.p_filesz, (off_t)ph.p_offset) != (ssize_t)ph.p_filesz ||
            interpreter[ph.p_filesz - 1]) break;
        kind = !strcmp(interpreter, "/system/bin/linker64") ||
            !strcmp(interpreter, "/apex/com.android.runtime/bin/linker64") ? 1 : 2;
        break;
    }
done:
    /* This fd never belongs to the DNS bridge. Calling its interposed close()
     * could take a mutex held by another thread while we are a vfork child. */
    syscall(SYS_close, fd);
    errno = saved;
    return kind;
}

static int execute(const char *path, char *const argv[], char *const envp[], unsigned depth);

static int execute_inner(const char *path, char *const argv[], char *const envp[], unsigned depth) {
    if (!next_execve) { errno = ENOSYS; return -1; }
    if (!path) { errno = EFAULT; return -1; }
    char mapped[PATH_MAX], script[256];
    const char *target = map_path(path, mapped);
    if (!target) return -1;
    /* Preserve a selected policy on direct self-spawn even if the application
     * constructs a fresh child environment. An explicit mode is still honored.
     * Persistent mode is independently read by the ELF constructor on exec. */
    if (termux_privacy_enabled() && is_claude(target) &&
        !env_value(envp, "CLAUDE_TERMUX_PRIVACY")) {
        size_t count;
        if (vector_length(envp, &count)) return -1;
        char *environment[count + 2];
        if (count) memcpy(environment, envp, count * sizeof(char *));
        environment[count] = "CLAUDE_TERMUX_PRIVACY=optional-off";
        environment[count + 1] = NULL;
        return execute(target, argv, environment, depth);
    }
    int kind = inspect(target, script);
    if (kind == 3) {
        if (depth == SCRIPT_LIMIT) { errno = ELOOP; return -1; }
        /* Interpreting a script must not circumvent its execute permission. */
        if (faccessat(AT_FDCWD, target, X_OK, AT_EACCESS)) return -1;
        char *interpreter = script + 2;
        while (*interpreter == ' ' || *interpreter == '\t') interpreter++;
        char *end = interpreter + strlen(interpreter);
        while (end > interpreter && (end[-1] == ' ' || end[-1] == '\t')) *--end = 0;
        char *argument = interpreter;
        while (*argument && *argument != ' ' && *argument != '\t') argument++;
        if (*argument) {
            *argument++ = 0;
            while (*argument == ' ' || *argument == '\t') argument++;
        }
        if (!*interpreter) { errno = ENOEXEC; return -1; }
        size_t count;
        if (vector_length(argv, &count)) return -1;
        char *arguments[count + 4];
        size_t out = 0;
        arguments[out++] = interpreter;
        if (*argument) arguments[out++] = argument;
        arguments[out++] = (char *)target;
        for (size_t i = 1; i < count; i++) arguments[out++] = argv[i];
        arguments[out] = NULL;
        return execute(interpreter, arguments, envp, depth + 1);
    }
    if (!kind) return next_execve(target, argv, envp);
#ifndef __BIONIC__
    /* No ABI transition: honor an explicitly supplied musl loader environment.
     * The constructor has already removed our inherited preload, if any. */
    if (kind == 2) return next_execve(target, argv, envp);
#endif
    size_t count;
    if (vector_length(envp, &count)) return -1;
    const char *preload = env_value(envp, "LD_PRELOAD");
    const char *libraries = env_value(envp, "LD_LIBRARY_PATH");
#ifndef __BIONIC__
    if (!preload) preload = env_value(envp, "CLAUDE_TERMUX_BIONIC_PRELOAD");
    if (!libraries) libraries = env_value(envp, "CLAUDE_TERMUX_BIONIC_LIBRARY_PATH");
#endif
    if (!preload && kind == 1) preload = TERMUX_PREFIX "/lib/libtermux-exec-ld-preload.so";
    if (preload) preload = without_our_preload(preload);
    size_t preload_size = preload ? strlen(preload) : 0;
    size_t libraries_size = libraries ? strlen(libraries) : 0;
    /* Bound stack use for these two rewritten values as well as the vectors.
     * The normal Linux 4 KiB-page exec string limit is 128 KiB. */
    if (preload_size > 131000 || libraries_size > 131000) { errno = E2BIG; return -1; }
    char preload_entry[preload_size + sizeof(BIONIC_EXEC_LIBRARY) + 40];
    char libraries_entry[libraries_size + 40];
    if (preload) {
        strcpy(preload_entry, kind == 1 ? "LD_PRELOAD=" BIONIC_EXEC_LIBRARY : "CLAUDE_TERMUX_BIONIC_PRELOAD=");
        if (kind == 1 && *preload) strcat(preload_entry, ":");
        strcat(preload_entry, preload);
    }
    if (libraries) {
        strcpy(libraries_entry, kind == 1 ? "LD_LIBRARY_PATH=" : "CLAUDE_TERMUX_BIONIC_LIBRARY_PATH=");
        strcat(libraries_entry, libraries);
    }
    char *environment[count + 5];
    size_t out = 0;
    for (size_t i = 0; i < count; i++) {
        if (!strncmp(envp[i], "LD_PRELOAD=", 11) || !strncmp(envp[i], "LD_LIBRARY_PATH=", 16)) continue;
        if (kind == 2 && ((preload && !strncmp(envp[i], "CLAUDE_TERMUX_BIONIC_PRELOAD=", sizeof("CLAUDE_TERMUX_BIONIC_PRELOAD=") - 1)) ||
            (libraries && !strncmp(envp[i], "CLAUDE_TERMUX_BIONIC_LIBRARY_PATH=", sizeof("CLAUDE_TERMUX_BIONIC_LIBRARY_PATH=") - 1)))) continue;
        environment[out++] = envp[i];
    }
    if (preload) environment[out++] = preload_entry;
    if (libraries) environment[out++] = libraries_entry;
    environment[out] = NULL;
    return next_execve(target, argv, environment);
}

static int execute(const char *path, char *const argv[], char *const envp[], unsigned depth) {
    int lease = depth ? -1 : lease_guard();
    if (lease == -2) return -1;
    int result = execute_inner(path, argv, envp, depth);
    int saved = errno;
    if (lease >= 0) syscall(SYS_close, lease);
    errno = saved;
    return result;
}

int execve(const char *path, char *const argv[], char *const envp[]) { return execute(path, argv, envp, 0); }
int execv(const char *path, char *const argv[]) { return execute(path, argv, environ, 0); }

static int shell_fallback(const char *path, char *const argv[], char *const envp[]) {
    size_t count;
    if (vector_length(argv, &count)) return -1;
    char *arguments[count + 3];
    arguments[0] = "sh";
    arguments[1] = (char *)path;
    for (size_t i = 1; i < count; i++) arguments[i + 1] = argv[i];
    arguments[count ? count + 1 : 2] = NULL;
    return execute(TERMUX_BIN "sh", arguments, envp, 0);
}

int execvpe(const char *file, char *const argv[], char *const envp[]) {
    if (!file || !*file) { errno = ENOENT; return -1; }
    if (strchr(file, '/')) {
        int result = execute(file, argv, envp, 0);
        return result < 0 && errno == ENOEXEC ? shell_fallback(file, argv, envp) : result;
    }
    const char *search = env_value(environ, "PATH");
    if (!search) search = TERMUX_BIN ":/system/bin";
    size_t length = strlen(file);
    int denied = 0;
    do {
        const char *end = strchr(search, ':');
        size_t dir = end ? (size_t)(end - search) : strlen(search);
        if (dir + length + 2 <= PATH_MAX) {
            char path[PATH_MAX];
            memcpy(path, search, dir);
            size_t offset = dir;
            if (dir) path[offset++] = '/';
            memcpy(path + offset, file, length + 1);
            execute(path, argv, envp, 0);
            if (errno == ENOEXEC) return shell_fallback(path, argv, envp);
            if (errno == EACCES) denied = 1;
            else if (errno != ENOENT && errno != ENOTDIR) return -1;
        }
        if (!end) break;
        search = end + 1;
    } while (1);
    errno = denied ? EACCES : ENOENT;
    return -1;
}
int execvp(const char *file, char *const argv[]) { return execvpe(file, argv, environ); }

static int exec_list(const char *file, const char *arg, va_list ap, int search, int explicit_env) {
    va_list copy;
    va_copy(copy, ap);
    size_t count = arg ? 1 : 0;
    if (arg) while (va_arg(copy, char *)) {
        if (++count == VECTOR_LIMIT) { va_end(copy); errno = E2BIG; return -1; }
    }
    va_end(copy);
    char *arguments[count + 1];
    if (arg) {
        arguments[0] = (char *)arg;
        for (size_t i = 1; i <= count; i++) arguments[i] = va_arg(ap, char *);
    } else arguments[0] = NULL;
    char *const *environment = explicit_env ? va_arg(ap, char **) : environ;
    return search ? execvpe(file, arguments, environment) : execute(file, arguments, environment, 0);
}
#define LIST_EXEC(name, search, explicit_env) \
int name(const char *file, const char *arg, ...) { \
    va_list ap; va_start(ap, arg); \
    int result = exec_list(file, arg, ap, search, explicit_env); \
    va_end(ap); return result; \
}
LIST_EXEC(execl, 0, 0)
LIST_EXEC(execlp, 1, 0)
LIST_EXEC(execle, 0, 1)

int fexecve(int fd, char *const argv[], char *const envp[]) {
    if (fcntl(fd, F_GETFD) < 0) return -1;
    char path[64];
    snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
    char script[256];
    if ((fcntl(fd, F_GETFD) & FD_CLOEXEC) && inspect(path, script) == 3) { errno = ENOENT; return -1; }
    return execute(path, argv, envp, 0);
}
