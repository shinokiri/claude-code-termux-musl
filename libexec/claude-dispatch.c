/* Bionic -> musl boundary. Preserve argv[0] for Claude's embedded tools. */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef CLAUDE_BINARY
#error CLAUDE_BINARY must be an absolute path
#endif

int main(int argc, char **argv) {
    (void)argc;
    unsetenv("LD_PRELOAD");
    unsetenv("LD_LIBRARY_PATH");
    execv(CLAUDE_BINARY, argv);
    perror("claude-termux: execute musl binary");
    return 126;
}
