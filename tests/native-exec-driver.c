/* Test the exported libc exec family from an actual Bionic tool process. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
extern char **environ;
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    char *const args[] = {"exec-fixture", "--version", "argument with spaces", "", "雪", "--key=one two", NULL};
    const char *mode = argv[1], *target = argv[2];
    if (!strcmp(mode, "execve")) execve(target, args, environ);
    else if (!strcmp(mode, "execv")) execv(target, args);
    else if (!strcmp(mode, "execvp")) execvp(target, args);
    else if (!strcmp(mode, "execvpe")) execvpe(target, args, environ);
    else if (!strcmp(mode, "execl")) execl(target, args[0], args[1], args[2], args[3], args[4], args[5], (char *)NULL);
    else if (!strcmp(mode, "execlp")) execlp(target, args[0], args[1], args[2], args[3], args[4], args[5], (char *)NULL);
    else if (!strcmp(mode, "execle")) execle(target, args[0], args[1], args[2], args[3], args[4], args[5], (char *)NULL, environ);
    else if (!strcmp(mode, "fexecve")) {
        int fd = open(target, O_RDONLY | O_CLOEXEC);
        if (fd < 0) { perror("open"); return 3; }
        int (*call)(int, char *const[], char *const[]) = dlsym(RTLD_DEFAULT, "fexecve");
        if (!call) return 5;
        call(fd, args, environ);
    } else return 4;
    perror(mode);
    return 1;
}
