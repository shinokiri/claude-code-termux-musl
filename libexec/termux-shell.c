/* Restore the Android environment only for Termux shell commands. */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef TERMUX_BASH
#error Compile with TERMUX_BASH, TERMUX_PRELOAD and CLAUDE_DISPATCH paths
#endif

int main(int argc, char **argv) {
    if (argc != 2) {
        fputs("termux-shell: expected one command string\n", stderr);
        return 2;
    }
    const char *preload = getenv("CLAUDE_TERMUX_BIONIC_PRELOAD");
    const char *libraries = getenv("CLAUDE_TERMUX_BIONIC_LIBRARY_PATH");
    setenv("LD_PRELOAD", preload && *preload ? preload : TERMUX_PRELOAD, 1);
    if (libraries && *libraries) setenv("LD_LIBRARY_PATH", libraries, 1);
    else unsetenv("LD_LIBRARY_PATH");
    setenv("CLAUDE_CODE_EXECPATH", CLAUDE_DISPATCH, 1);
    execl(TERMUX_BASH, TERMUX_BASH, "-c", argv[1], (char *)NULL);
    perror("termux-shell: execute bash");
    return 126;
}
