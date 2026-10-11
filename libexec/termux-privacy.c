/* SPDX-License-Identifier: MIT
 * Load the selected reporting policy before Bun starts, including direct
 * self-spawns with an empty environment. Policy is outside immutable releases.
 * This does not constrain arbitrary networking by native code or other tools.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <unistd.h>

static int privacy_enabled;
int termux_privacy_enabled(void) { return privacy_enabled; }

static void privacy_failure(const char *reason) {
    const char *prefix = "claude-termux: privacy policy unavailable: ";
    syscall(SYS_write, STDERR_FILENO, prefix, strlen(prefix));
    syscall(SYS_write, STDERR_FILENO, reason, strlen(reason));
    syscall(SYS_write, STDERR_FILENO, "\n", 1);
    _exit(78);
}

static void privacy_env(const char *name, const char *value) {
    if (setenv(name, value, 1)) privacy_failure("cannot initialize environment");
}

__attribute__((constructor)) static void privacy_initialize(void) {
    char selected[64] = "standard";
    const char *mode = getenv("CLAUDE_TERMUX_PRIVACY");
    if (!mode) {
        int fd = (int)syscall(SYS_openat, AT_FDCWD, PRIVACY_MODE_FILE, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0);
        if (fd >= 0) {
            struct stat info;
            if (fstat(fd, &info) || !S_ISREG(info.st_mode)) privacy_failure("mode file is not regular");
            ssize_t count = syscall(SYS_read, fd, selected, sizeof(selected) - 1);
            syscall(SYS_close, fd);
            if (count <= 0 || count == sizeof(selected) - 1) privacy_failure("invalid mode file");
            selected[count] = 0;
            if (selected[count - 1] == '\n') selected[count - 1] = 0;
        } else if (errno != ENOENT) privacy_failure("cannot read mode file");
        mode = selected;
    }
    if (strcmp(mode, "standard") && strcmp(mode, "optional-off")) privacy_failure("expected standard or optional-off");
    privacy_env("CLAUDE_TERMUX_PRIVACY_ACTIVE", mode);
    if (!strcmp(mode, "standard")) return;
    privacy_enabled = 1;
    privacy_env("CLAUDE_TERMUX_PRIVACY", "optional-off");
    if (access(PRIVACY_PRELOAD, R_OK)) privacy_failure("preload file missing");
    privacy_env("CLAUDE_TERMUX_PRIVACY_TARGET", CLAUDE_BINARY);
    privacy_env("CLAUDE_TERMUX_PRIVACY_PRELOAD", PRIVACY_PRELOAD);
    const char *old = getenv("BUN_OPTIONS");
    const char *leading = "--preload " PRIVACY_PRELOAD;
    size_t leading_size = strlen(leading);
    if (old && !strncmp(old, leading, leading_size) &&
        (!old[leading_size] || old[leading_size] == ' ')) return;
    /* Bun's BUN_OPTIONS parser does not shell-unquote --preload values. */
    if (strpbrk(PRIVACY_PRELOAD, " \t\r\n")) privacy_failure("preload path contains whitespace");
    size_t needed = strlen(PRIVACY_PRELOAD) + (old ? strlen(old) : 0) + 32;
    char *options = malloc(needed);
    if (!options) privacy_failure("out of memory");
    char *p = stpcpy(options, "--preload ");
    p = stpcpy(p, PRIVACY_PRELOAD);
    if (old && *old) { *p++ = ' '; p = stpcpy(p, old); }
    *p = 0;
    privacy_env("BUN_OPTIONS", options);
    free(options);
}
