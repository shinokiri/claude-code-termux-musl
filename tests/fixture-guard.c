#define _GNU_SOURCE
/* Synthetic-test accident guard. This is NOT a hostile-code sandbox.
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
#include <signal.h>
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
#include <time.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { perror(#x); exit(125); } } while (0)
#define NOTIFY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_##n, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_USER_NOTIF)
#define DENY(n) BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K, SYS_##n, 0, 1), BPF_STMT(BPF_RET|BPF_K, SECCOMP_RET_ERRNO|EPERM)

static volatile sig_atomic_t interrupted;
static void interrupt_handler(int signal_number) { interrupted=signal_number; }

static double monotonic(void) {
    struct timespec value;
    CHECK(clock_gettime(CLOCK_MONOTONIC,&value)==0);
    return value.tv_sec+value.tv_nsec/1e9;
}

/* The subreaper owns these children. Once a parent exits, its surviving
 * descendants become direct children and are picked up on the next pass.
 * A pidfd plus a second parent check prevents signalling an unrelated reused PID.
 */
static pid_t parent_of(pid_t pid) {
    char path[64], line[4096], state;
    snprintf(path,sizeof(path),"/proc/%d/stat",pid);
    FILE *stat=fopen(path,"r");
    if (!stat) return -1;
    pid_t parent=-1;
    if (fgets(line,sizeof(line),stat)) {
        char *end=strrchr(line,')');
        if (!end || sscanf(end+1," %c %d",&state,&parent)!=2 || state=='Z') parent=-1;
    }
    fclose(stat);
    return parent;
}

static unsigned signal_children(int signal_number) {
    /* Android kernels need not expose /proc/<pid>/task/<tid>/children. */
    DIR *processes=opendir("/proc");
    CHECK(processes);
    struct dirent *entry;
    unsigned count=0;
    while ((entry=readdir(processes))) {
        char *end;
        long number=strtol(entry->d_name,&end,10);
        if (*end || number<=0 || number>2147483647) continue;
        pid_t pid=(pid_t)number;
        if (parent_of(pid)!=getpid()) continue;
        int fd=syscall(SYS_pidfd_open,pid,0);
        if (fd<0 && errno==ESRCH) continue;
        CHECK(fd>=0);
        if (parent_of(pid)==getpid()) {
            int result=syscall(SYS_pidfd_send_signal,fd,signal_number,NULL,0);
            CHECK(result==0 || errno==ESRCH);
            if (!result) count++;
        }
        close(fd);
    }
    closedir(processes);
    return count;
}

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

static int address_allowed(pid_t pid, unsigned long pointer, unsigned long size, const char **reason) {
    if (!pointer) return 1; /* A connected send: its connect was inspected. */
    struct sockaddr_storage storage={0};
    if (size<sizeof(sa_family_t) || size>sizeof(storage) || !read_memory(pid,pointer,&storage,size)) return 0;
    if (storage.ss_family==AF_INET && size>=sizeof(struct sockaddr_in)) {
        *reason="ipv4-destination";
        struct sockaddr_in *a=(void *)&storage;
        return ntohl(a->sin_addr.s_addr)==0x7f000001;
    }
    if (storage.ss_family==AF_INET6 && size>=sizeof(struct sockaddr_in6)) {
        *reason="ipv6-destination";
        struct sockaddr_in6 *a=(void *)&storage;
        return IN6_IS_ADDR_LOOPBACK(&a->sin6_addr);
    }
    if (storage.ss_family==AF_UNIX) {
        struct sockaddr_un *a=(void *)&storage;
        /* Bionic's real resolver could delegate a DNS query outside the tree. */
        if (!strncmp(a->sun_path,"/dev/socket/dnsproxyd",sizeof(a->sun_path))) {
            *reason="system-dns-service";
            return 0;
        }
        *reason="local-ipc";
        return 1;
    }
    return storage.ss_family==AF_UNSPEC; /* UDP disconnect. */
}

static int message_allowed(pid_t pid, unsigned long pointer, const char **reason) {
    struct msghdr message;
    return read_memory(pid,pointer,&message,sizeof(message)) &&
           address_allowed(pid,(unsigned long)message.msg_name,message.msg_namelen,reason);
}

static int decision(struct seccomp_notif *req, const char **reason) {
    const __u64 *args=req->data.args;
    int n=req->data.nr;
    *reason="network-address";
    if (n==SYS_connect) return address_allowed(req->pid,args[1],args[2],reason);
    if (n==SYS_sendto) return address_allowed(req->pid,args[4],args[5],reason);
    if (n==SYS_sendmsg) return message_allowed(req->pid,args[1],reason);
    if (n==SYS_sendmmsg) {
        if (args[2]>1024) return 0;
        for (unsigned long i=0;i<args[2];i++)
            if (!message_allowed(req->pid,args[1]+i*sizeof(struct mmsghdr),reason)) return 0;
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
    pid_t owner=getppid();
    struct sigaction action={.sa_handler=interrupt_handler};
    CHECK(sigemptyset(&action.sa_mask)==0);
    CHECK(sigaction(SIGTERM,&action,NULL)==0 && sigaction(SIGINT,&action,NULL)==0);
    CHECK(prctl(PR_SET_PDEATHSIG,SIGTERM,0,0,0)==0);
    CHECK(getppid()==owner);
    double limit=600, started=monotonic();
    const char *configured=getenv("TERMUX_FIXTURE_TIMEOUT");
    if (configured) {
        char *end;
        limit=strtod(configured,&end);
        CHECK(!*end && limit>=1 && limit<=3600);
    }
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
    pid_t broker=getpid(), child=fork();
    CHECK(child>=0);
    if (!child) {
        CHECK(prctl(PR_SET_PDEATHSIG,SIGKILL,0,0,0)==0);
        CHECK(getppid()==broker);
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
        char marker[32];
        snprintf(marker,sizeof(marker),"%d",broker);
        CHECK(setenv("TERMUX_FIXTURE_BROKER",marker,1)==0);
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
    fprintf(log,"{\"event\":\"ready\"}\n");
    close(ready[0]);close(go[1]);close(pidfd);
    struct seccomp_notif_sizes sizes;
    CHECK(syscall(SYS_seccomp,SECCOMP_GET_NOTIF_SIZES,0,&sizes)==0);
    struct seccomp_notif *req=calloc(1,sizes.seccomp_notif);
    struct seccomp_notif_resp *resp=calloc(1,sizes.seccomp_notif_resp);
    CHECK(req && resp);
    struct pollfd pollfd={listener,POLLIN,0};
    int status=0, child_finished=0, timed_out=0;
    double stopping=0;
    unsigned cleanup_signals=0;
    while (1) {
        int reaped_status;
        pid_t reaped;
        while ((reaped=waitpid(-1,&reaped_status,WNOHANG))>0)
            if (reaped==child) {status=reaped_status;child_finished=1;}
        if (child_finished && reaped<0 && errno==ECHILD) break;
        double now=monotonic();
        if (now-started>=limit) timed_out=1;
        if (child_finished || interrupted || timed_out) {
            if (!stopping) stopping=now;
            cleanup_signals+=signal_children(now-stopping<2?SIGTERM:SIGKILL);
        }
        int ready_count=poll(&pollfd,1,250);
        if (ready_count<0) { if (errno==EINTR) continue; CHECK(0); }
        if (!ready_count) continue;
        if (!(pollfd.revents&POLLIN) && (pollfd.revents&POLLHUP)) {
            /* The filter can vanish before waitpid sees every final exit. */
            struct timespec pause={0,10000000};
            nanosleep(&pause,NULL);
            continue;
        }
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
    fprintf(log,"{\"event\":\"finished\",\"cleanup_signals\":%u}\n",cleanup_signals);
    close(listener);fclose(log);free(req);free(resp);
    if (timed_out) return 124;
    if (interrupted) return 128+interrupted;
    return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
