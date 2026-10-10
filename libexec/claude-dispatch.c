/* Bionic -> musl boundary. Preserve argv[0] for Claude's embedded tools. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef CLAUDE_BINARY
#error CLAUDE_BINARY must be an absolute path
#endif

int main(int argc, char **argv) {
    (void)argc;
    unsetenv("LD_PRELOAD");
    unsetenv("LD_LIBRARY_PATH");
#ifdef CLAUDE_DNS_LIBRARY
    const char *mode = getenv("CLAUDE_TERMUX_DNS_MODE");
    if (!mode || strcmp(mode, "native") == 0) {
        if (setenv("LD_PRELOAD", CLAUDE_DNS_LIBRARY, 1)) {
            perror("claude-termux: load Android DNS compatibility library");
            return 126;
        }
    }
#endif
    execv(CLAUDE_BINARY, argv);
    perror("claude-termux: execute musl binary");
    return 126;
}
