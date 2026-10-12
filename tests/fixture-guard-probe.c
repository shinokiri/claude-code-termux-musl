#define _GNU_SOURCE
/* Both destinations are loopback, even if the guard under test is broken. */
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { perror(#x); return 1; } } while (0)

int main(int argc, char **argv) {
    CHECK(argc==5);
    int denied=!strcmp(argv[1],"deny");
    struct sockaddr_in target={.sin_family=AF_INET,.sin_port=htons(atoi(argv[3]))};
    CHECK(inet_pton(AF_INET,argv[2],&target.sin_addr)==1);
    int tcp=socket(AF_INET,SOCK_STREAM,0), udp=socket(AF_INET,SOCK_DGRAM,0);
    CHECK(tcp>=0 && udp>=0);
    long result=syscall(SYS_connect,tcp,&target,sizeof(target));
    if (denied) CHECK(result==-1 && errno==EPERM);
    else {
        CHECK(result==0);
        CHECK(syscall(SYS_write,tcp,"TCP",3)==3);
    }
    close(tcp);
    target.sin_port=htons(atoi(argv[4]));
    result=syscall(SYS_sendto,udp,"A",1,0,&target,sizeof(target));
    CHECK(denied?(result==-1 && errno==EPERM):result==1);
    struct iovec vector={"B",1};
    struct msghdr message={.msg_name=&target,.msg_namelen=sizeof(target),.msg_iov=&vector,.msg_iovlen=1};
    result=syscall(SYS_sendmsg,udp,&message,0);
    CHECK(denied?(result==-1 && errno==EPERM):result==1);
    vector.iov_base="C";
    struct mmsghdr batch={.msg_hdr=message};
    result=syscall(SYS_sendmmsg,udp,&batch,1,0);
    CHECK(denied?(result==-1 && errno==EPERM):result==1);
    close(udp);
    puts("PROBE_OK");
    return 0;
}
