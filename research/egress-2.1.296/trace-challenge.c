#define _GNU_SOURCE
/* Synthetic loopback traffic which deliberately bypasses libc write interposition. */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/sendfile.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { perror(#x); exit(1); } } while (0)

__attribute__((noinline)) static long inline_write(int fd, const void *data, size_t size) {
#if defined(__aarch64__)
    register long x0 __asm__("x0") = fd;
    register long x1 __asm__("x1") = (long)data;
    register long x2 __asm__("x2") = size;
    register long x8 __asm__("x8") = 64;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory", "cc");
    return x0;
#else
#error This challenge intentionally targets the installed AArch64 artifact's ABI.
#endif
}

static void text_write(int fd, const char *text) {
    size_t size = strlen(text);
    CHECK(write(fd, text, size) == (ssize_t)size);
}

static void wait_ok(pid_t pid) {
    int status;
    CHECK(waitpid(pid, &status, 0) == pid);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 0);
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--negative-guard")) {
        struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=htons(443)};
        CHECK(inet_pton(AF_INET, "192.0.2.1", &address.sin_addr)==1);
        int tcp=socket(AF_INET,SOCK_STREAM,0), udp=socket(AF_INET,SOCK_DGRAM,0);
        CHECK(tcp>=0 && udp>=0);
        CHECK(syscall(SYS_connect,tcp,&address,sizeof(address))==-1 && errno==EPERM);
        CHECK(sendto(udp,"SYNTHETIC",9,0,(struct sockaddr *)&address,sizeof(address))==-1 && errno==EPERM);
        struct iovec iov={"SYNTHETIC",9};
        struct msghdr message={.msg_name=&address,.msg_namelen=sizeof(address),.msg_iov=&iov,.msg_iovlen=1};
        CHECK(sendmsg(udp,&message,0)==-1 && errno==EPERM);
        struct mmsghdr batch={.msg_hdr=message};
        CHECK(sendmmsg(udp,&batch,1,0)==-1 && errno==EPERM);
        execlp("ssh","ssh","-V",NULL);
        CHECK(errno==EPERM);
        puts("GUARD_NEGATIVE_DONE");
        return 0;
    }
    if (argc == 3 && !strcmp(argv[1], "--inherited")) {
        text_write(atoi(argv[2]), "EXEC_INHERITED\n");
        return 0;
    }
    CHECK(argc == 3);
    printf("INLINE_WRITE_ADDRESS=%p\n", (void *)inline_write);
    fflush(stdout);
    int ipc[2];
    CHECK(socketpair(AF_UNIX, SOCK_SEQPACKET, 0, ipc) == 0);
    pid_t worker = fork();
    CHECK(worker >= 0);
    if (!worker) {
        CHECK(close(ipc[0]) == 0);
        char byte, control[CMSG_SPACE(sizeof(int))];
        struct iovec iov = {&byte, 1};
        struct msghdr msg = {.msg_iov = &iov, .msg_iovlen = 1,
                            .msg_control = control, .msg_controllen = sizeof(control)};
        CHECK(recvmsg(ipc[1], &msg, 0) == 1);
        struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
        CHECK(cmsg && cmsg->cmsg_level == SOL_SOCKET && cmsg->cmsg_type == SCM_RIGHTS);
        CHECK(!(msg.msg_flags & MSG_CTRUNC));
        int received;
        memcpy(&received, CMSG_DATA(cmsg), sizeof(received));
        text_write(received, "SCM_RIGHTS_CHILD\n");
        close(received);
        close(ipc[1]);
        _exit(0);
    }
    CHECK(close(ipc[1]) == 0);
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(atoi(argv[1]))};
    CHECK(inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr) == 1);
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(fd >= 0);
    CHECK(connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0);
    struct sockaddr_in local;
    socklen_t local_size = sizeof(local);
    CHECK(getsockname(fd, (struct sockaddr *)&local, &local_size) == 0);
    text_write(fd, "LIBC_WRITE\n");
    const char direct[] = "INLINE_SVC_WRITE\n";
    CHECK(inline_write(fd, direct, sizeof(direct)-1) == sizeof(direct)-1);
    struct iovec vector[] = {{"VECTOR_", 7}, {"WRITE\n", 6}};
    CHECK(writev(fd, vector, 2) == 13);
    int file = syscall(SYS_memfd_create, "synthetic-sendfile", MFD_CLOEXEC);
    CHECK(file >= 0);
    text_write(file, "SENDFILE\n");
    CHECK(lseek(file, 0, SEEK_SET) == 0);
    CHECK(sendfile(fd, file, NULL, 9) == 9);
    close(file);
    char byte = 'F', control[CMSG_SPACE(sizeof(int))] = {0};
    struct iovec iov = {&byte, 1};
    struct msghdr msg = {.msg_iov = &iov, .msg_iovlen = 1,
                        .msg_control = control, .msg_controllen = sizeof(control)};
    struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
    cmsg->cmsg_level = SOL_SOCKET;
    cmsg->cmsg_type = SCM_RIGHTS;
    cmsg->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(cmsg), &fd, sizeof(fd));
    CHECK(sendmsg(ipc[0], &msg, 0) == 1);
    wait_ok(worker);
    close(ipc[0]);
    pid_t child = fork();
    CHECK(child >= 0);
    if (!child) {
        CHECK(setsid() >= 0);
        CHECK(dup2(fd, 200) == 200);
        close(fd);
        execl(argv[0], argv[0], "--inherited", "200", NULL);
        _exit(2);
    }
    wait_ok(child);
    close(fd);
    int udp = socket(AF_INET, SOCK_DGRAM, 0);
    CHECK(udp >= 0);
    addr.sin_port = htons(atoi(argv[2]));
    CHECK(sendto(udp, "UDP_SENDTO", 10, 0, (struct sockaddr *)&addr, sizeof(addr)) == 10);
    struct iovec items[] = {{"MMSG_1", 6}, {"MMSG_2", 6}};
    struct mmsghdr batch[2] = {0};
    for (int i = 0; i < 2; i++) {
        batch[i].msg_hdr.msg_name = &addr;
        batch[i].msg_hdr.msg_namelen = sizeof(addr);
        batch[i].msg_hdr.msg_iov = &items[i];
        batch[i].msg_hdr.msg_iovlen = 1;
    }
    CHECK(sendmmsg(udp, batch, 2, 0) == 2);
    close(udp);
    puts("TRACE_CHALLENGE_DONE");
    return 0;
}
