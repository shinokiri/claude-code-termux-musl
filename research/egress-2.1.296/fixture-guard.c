#define _GNU_SOURCE
/* Research-only accident guard. This is NOT a hostile-code sandbox.
 * seccomp user notifications inspect pointer arguments then CONTINUE; another
 * thread could mutate them. The trace remains the independent audit record.
 * No production launcher/library uses this program.
 */
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <poll.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { perror(#x); exit(125); } } while (0)
#define NOTIFY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_##n, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_USER_NOTIF)
#define DENY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_##n, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM)

static int read_memory(pid_t pid, unsigned long address, void *out, size_t size) {
    struct iovec local={out,size}, remote={(void *)address,size};
    return syscall(SYS_process_vm_readv,pid,&local,1,&remote,1,0)==(ssize_t)size;
}

static int read_text(pid_t pid, unsigned long address, char *out, size_t capacity) {
    for (size_t i=0;i<capacity;) {
        size_t size=64, page_left=4096-(address+i)%4096;
        if (size>capacity-i) size=capacity-i;
        if (size>page_left) size=page_left;
        if (!read_memory(pid,address+i,out+i,size)) return 0;
        if (memchr(out+i,0,size)) return 1;
        i+=size;
    }
    return 0;
}

static int address_allowed(pid_t pid, unsigned long pointer, unsigned long size) {
    if (!pointer) return 1; /* A connected send: its connect was inspected. */
    struct sockaddr_storage storage={0};
    if (size<sizeof(sa_family_t) || size>sizeof(storage) || !read_memory(pid,pointer,&storage,size)) return 0;
    if (storage.ss_family==AF_INET && size>=sizeof(struct sockaddr_in)) {
        struct sockaddr_in *a=(void *)&storage;
        return ntohl(a->sin_addr.s_addr)==0x7f000001;
    }
    if (storage.ss_family==AF_INET6 && size>=sizeof(struct sockaddr_in6)) {
        struct sockaddr_in6 *a=(void *)&storage;
        return IN6_IS_ADDR_LOOPBACK(&a->sin6_addr);
    }
    if (storage.ss_family==AF_UNIX) {
        struct sockaddr_un *a=(void *)&storage;
        /* Bionic's real resolver could delegate a DNS query outside the tree. */
        return strncmp(a->sun_path,"/dev/socket/dnsproxyd",sizeof(a->sun_path))!=0;
    }
    return storage.ss_family==AF_UNSPEC; /* UDP disconnect. */
}

static int message_allowed(pid_t pid, unsigned long pointer) {
    struct msghdr message;
    return read_memory(pid,pointer,&message,sizeof(message)) &&
           address_allowed(pid,(unsigned long)message.msg_name,message.msg_namelen);
}

static int decision(struct seccomp_notif *req, const char **reason) {
    unsigned long *args=(void *)req->data.args;
    int n=req->data.nr;
    *reason="loopback-or-local-ipc";
    if (n==SYS_connect) return address_allowed(req->pid,args[1],args[2]);
    if (n==SYS_sendto) return address_allowed(req->pid,args[4],args[5]);
    if (n==SYS_sendmsg) return message_allowed(req->pid,args[1]);
    if (n==SYS_sendmmsg) {
        if (args[2]>1024) return 0;
        for (unsigned long i=0;i<args[2];i++)
            if (!message_allowed(req->pid,args[1]+i*sizeof(struct mmsghdr))) return 0;
        return 1;
    }
    if (n==SYS_socket) return (args[0]==AF_INET || args[0]==AF_INET6 || args[0]==AF_UNIX) && (args[1]&0xf)!=SOCK_RAW;
    if (n==SYS_execve || n==SYS_execveat) {
        char path[4096];
        *reason="exec";
        if (!read_text(req->pid,args[n==SYS_execve?0:1],path,sizeof(path))) return 0;
        char *base=strrchr(path,'/');
        base=base?base+1:path;
        if (!strcmp(base,"ssh")) { *reason="real-ssh-and-default-identity-disabled-in-fixture"; return 0; }
        return 1;
    }
    *reason="unexpected-notification";
    return 0;
}

int main(int argc, char **argv) {
    CHECK(argc>=4 && !strcmp(argv[2],"--"));
    /* Refuse an already-open network channel before installing the filter. */
    DIR *directory=opendir("/proc/self/fd");
    CHECK(directory);
    struct dirent *entry;
    while ((entry=readdir(directory))) {
        char path[256], target[256];
        int n=snprintf(path,sizeof(path),"/proc/self/fd/%s",entry->d_name);
        CHECK(n>0 && (size_t)n<sizeof(path));
        ssize_t size=readlink(path,target,sizeof(target)-1);
        if (size>0) { target[size]=0; CHECK(strncmp(target,"socket:[",8)!=0); }
    }
    closedir(directory);
    FILE *log=fopen(argv[1],"wx");
    CHECK(log);
    setbuf(log,NULL);
    CHECK(prctl(PR_SET_CHILD_SUBREAPER,1,0,0,0)==0);
    int ready[2], go[2];
    CHECK(pipe(ready)==0 && pipe(go)==0);
    pid_t child=fork();
    CHECK(child>=0);
    if (!child) {
        close(ready[0]); close(go[1]); fclose(log);
        struct sock_filter filter[]={
            BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,arch)),
            BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,AUDIT_ARCH_AARCH64,1,0),
            BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_KILL_PROCESS),
            BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
            NOTIFY(socket),NOTIFY(connect),NOTIFY(sendto),NOTIFY(sendmsg),NOTIFY(sendmmsg),NOTIFY(execve),NOTIFY(execveat),
            DENY(io_uring_setup),DENY(pidfd_getfd),DENY(ptrace),DENY(bpf),
            BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)
        };
        struct sock_fprog program={sizeof(filter)/sizeof(filter[0]),filter};
        CHECK(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)==0);
        int listener=syscall(SYS_seccomp,SECCOMP_SET_MODE_FILTER,SECCOMP_FILTER_FLAG_NEW_LISTENER,&program);
        CHECK(listener>=0);
        CHECK(write(ready[1],&listener,sizeof(listener))==sizeof(listener));
        char ack;
        CHECK(read(go[0],&ack,1)==1);
        close(listener);close(ready[1]);close(go[0]);
        execvp(argv[3],argv+3);
        perror("guard execvp");_exit(125);
    }
    close(ready[1]);close(go[0]);
    int foreign=-1;
    CHECK(read(ready[0],&foreign,sizeof(foreign))==sizeof(foreign));
    int pidfd=syscall(SYS_pidfd_open,child,0);
    CHECK(pidfd>=0);
    int listener=syscall(SYS_pidfd_getfd,pidfd,foreign,0);
    CHECK(listener>=0);
    CHECK(write(go[1],"G",1)==1);
    close(ready[0]);close(go[1]);close(pidfd);
    struct seccomp_notif_sizes sizes;
    CHECK(syscall(SYS_seccomp,SECCOMP_GET_NOTIF_SIZES,0,&sizes)==0);
    struct seccomp_notif *req=calloc(1,sizes.seccomp_notif);
    struct seccomp_notif_resp *resp=calloc(1,sizes.seccomp_notif_resp);
    CHECK(req && resp);
    struct pollfd pollfd={listener,POLLIN,0};
    int status=0, child_finished=0;
    while (1) {
        int reaped_status;
        pid_t reaped;
        while ((reaped=waitpid(-1,&reaped_status,WNOHANG))>0)
            if (reaped==child) {status=reaped_status;child_finished=1;}
        if (child_finished && reaped<0 && errno==ECHILD) break;
        int ready_count=poll(&pollfd,1,250);
        if (ready_count<0) { if (errno==EINTR) continue; CHECK(0); }
        if (!ready_count) continue;
        if (!(pollfd.revents&POLLIN) && (pollfd.revents&POLLHUP)) break;
        memset(req,0,sizes.seccomp_notif);
        if (ioctl(listener,SECCOMP_IOCTL_NOTIF_RECV,req)<0) {
            if (errno==EINTR || errno==ENOENT) continue;
            CHECK(0);
        }
        const char *reason;
        int allow=decision(req,&reason);
        if (ioctl(listener,SECCOMP_IOCTL_NOTIF_ID_VALID,&req->id)<0) continue;
        fprintf(log,"{\"tid\":%u,\"syscall\":%d,\"allowed\":%s,\"reason\":\"%s\"}\n",req->pid,req->data.nr,allow?"true":"false",reason);
        memset(resp,0,sizes.seccomp_notif_resp);
        resp->id=req->id;
        if (allow) resp->flags=SECCOMP_USER_NOTIF_FLAG_CONTINUE;
        else resp->error=-EPERM;
        if (ioctl(listener,SECCOMP_IOCTL_NOTIF_SEND,resp)<0 && errno!=ENOENT && errno!=EINTR) CHECK(0);
    }
    close(listener);fclose(log);free(req);free(resp);
    if (!child_finished) CHECK(waitpid(child,&status,0)==child);
    return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
