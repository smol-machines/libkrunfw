/* Source extraction test: docs/tsi-udp-routing.md. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#define EPOLLIN 1
#define EPOLLRDNORM 64
#define AF_VSOCK 40
#define AF_INET 2
#define PF_INET 2
struct net {int unused;};
static struct net init_net, child_net;
static struct net *current_net=&child_net;
#define SS_CONNECTED 1
#define S_INET 1
#define S_HYBRID 0
#define S_VSOCK 2
#define SOCK_DGRAM 2
#define GFP_KERNEL 0
#define VMADDR_CID_HOST 2
#define TSI_SENDTO_ADDR 8
#define TSI_SENDTO_DATA 9
#define fallthrough ((void)0)
#define pr_debug(...) ((void)0)
struct sockaddr {uint16_t family; char data[14];};
struct sockaddr_in {uint16_t sin_family,sin_port;struct {uint32_t s_addr;} sin_addr;char zero[8];};
struct sockaddr_storage {uint16_t family; char data[126];};
struct sockaddr_vm {uint16_t svm_family, reserved; uint32_t svm_port,svm_cid; uint8_t svm_flags; char zero[3];};
struct msghdr {void *msg_name; int msg_namelen; int msg_iter;};
struct sock {int sk_type; int sk_err;};
struct file {int unused;};
typedef int poll_table;
typedef unsigned int __poll_t;
struct socket;
struct ops {int (*sendmsg)(struct socket *,struct msghdr *,size_t); __poll_t (*poll)(struct file *,struct socket *,poll_table *); int (*recvmsg)(struct socket *,struct msghdr *,size_t,int);};
struct socket {struct sock *sk; struct ops *ops; int state; unsigned ready; int receive_calls;};
struct tsi_sock {struct sock sk;struct socket *isocket,*vsocket,*csocket;int status,family;void *sendto_addr;int sendto_addr_len;int svm_port;};
struct tsi_sendto_addr {int svm_port,addr_len;struct sockaddr_storage addr;};
static struct tsi_sock *tsi_sk(struct sock *s){return (struct tsi_sock *)s;}
static void lock_sock(struct sock *s){(void)s;}
static void release_sock(struct sock *s){(void)s;}
static void *kmalloc(size_t n,int flags){(void)flags;return malloc(n);}
static void iov_iter_revert(int *i,size_t n){(void)i;(void)n;}
static int local_listener;
static bool tsi_has_udp_listener(struct sock *s,void *a,int n){(void)s;(void)a;(void)n;return local_listener;}
static int tsi_check_addr_len(struct tsi_sock *t,int n){(void)t;return n==16?0:-EINVAL;}
static int tsi_create_proxy(struct tsi_sock *t,int type){(void)type;t->svm_port=123;return 0;}
static int tsi_control_sendmsg(struct socket *s,int p,void *d,int n){(void)s;(void)p;(void)d;return n;}
static int inet_calls,vsock_calls,local_error,dnat_deliveries;
static int dnat_case;
static int inet_send(struct socket *s,struct msghdr *m,size_t n){(void)s;inet_calls++;if(dnat_case){struct sockaddr_in *a=m->msg_name;if(!a || a->sin_port!=0x3500 || a->sin_addr.s_addr!=0x0b00007f)return -EINVAL;dnat_deliveries++;}return local_error?local_error:(int)n;}
/* Exact relevant contract of patched vsock_dgram_sendmsg: a named valid
 * AF_VSOCK peer OR a connected underlying vsock is required, else -EINVAL. */
static int vsock_send(struct socket *s,struct msghdr *m,size_t n){
 vsock_calls++;
 struct sockaddr_vm *a=m->msg_name;
 if(a && m->msg_namelen>=16 && a->svm_family==AF_VSOCK && !(a->svm_flags & ~1))return (int)n;
 return s->state?(int)n:-EINVAL;
}
static __poll_t writable_poll(struct file *f,struct socket *s,poll_table *w){(void)f;(void)w;return s->ready;}
static int owned_recv(struct socket *s,struct msghdr *m,size_t n,int flags){(void)m;(void)n;(void)flags;s->receive_calls++;if(!(s->ready & EPOLLIN))return -EAGAIN;s->ready &= ~EPOLLIN;return 29;}
#include "recvmsg-original.c"
#include "poll-under-test.c"
#define sock_net(sk) ((void)(sk),current_net)
#define ipv4_is_loopback(addr) (((addr)&0xff)==127)
#include "sendmsg-under-test.c"
int main(int argc,char **argv){
 if(argc!=2)return 2;
 struct ops io={inet_send,writable_poll,owned_recv},vo={vsock_send,writable_poll,owned_recv};
 struct sock inet_sk={0},vsock_sk={0};
 struct socket inet={&inet_sk,&io,0,4,0},vsock={&vsock_sk,&vo,0,4,0};
 struct sockaddr peer={2,{0}};
 struct tsi_sock t={.sk={.sk_type=SOCK_DGRAM},.isocket=&inet,.vsocket=&vsock,.status=S_HYBRID,.family=2,.sendto_addr=&peer,.sendto_addr_len=16};
 struct socket outer={&t.sk,0,1,0,0};
 if(!strncmp(argv[1],"dnat-",5) || !strcmp(argv[1],"loopback-unbound") || !strcmp(argv[1],"loopback-init")){
  struct sockaddr_in loop={.sin_family=2,.sin_port=0x3500,.sin_addr={0x0b00007f}};
  t.sendto_addr=&loop;local_listener=0;dnat_case=1;
  int initial_namespace=!strcmp(argv[1],"loopback-init"),bad=!strcmp(argv[1],"loopback-unbound");
  if(initial_namespace)current_net=&init_net;
  if(bad)local_error=-ECONNREFUSED;
  for(int i=0;i<2;i++){
   struct sockaddr_in target=loop;int connected=!strcmp(argv[1],"dnat-connected");
   struct msghdr msg={.msg_name=connected?NULL:&target,.msg_namelen=connected?0:16};
   tsi_poll(NULL,&outer,NULL);int result=tsi_dgram_sendmsg(&outer,&msg,29);
   printf("loopback result=%d inet=%d proxy=%d dnat=%d\n",result,inet_calls,vsock_calls,dnat_deliveries);
   if(result!=(bad?-ECONNREFUSED:29))return 1;
  }
  return initial_namespace?(inet_calls==0 && vsock_calls==2?0:1):(inet_calls==2 && vsock_calls==0 && dnat_deliveries==2?0:1);
 }
 if(!strcmp(argv[1],"poll-error-only")){inet.ready=8;vsock.ready=16;unsigned events=tsi_poll(NULL,&outer,NULL);printf("events=%u state=%d\n",events,t.status);return events==24 && t.status==S_HYBRID?0:1;}
 if(!strncmp(argv[1],"receive-",8)){
  int local=!strcmp(argv[1],"receive-local"),both=!strcmp(argv[1],"receive-both");
  inet.ready=4|((local||both)?EPOLLIN:0);vsock.ready=4|((!local)?EPOLLIN:0);
  for(int round=0;round<(both?2:1);round++){
   unsigned events=tsi_poll(NULL,&outer,NULL);struct msghdr msg={0};
   if(!(events&EPOLLIN)){printf("missing readable: events=%u status=%d\n",events,t.status);return 1;}
   int result=tsi_dgram_recvmsg(&outer,&msg,29,0);
   printf("receive=%d inet=%d vsock=%d state=%d\n",result,inet.receive_calls,vsock.receive_calls,t.status);
   if(result!=29)return 1;
  }
  return inet.receive_calls==(local||both?1:0) && vsock.receive_calls==(!local?1:0)?0:1;
 }
 int connected=!strcmp(argv[1],"connected") || !strcmp(argv[1],"connected-local") || !strcmp(argv[1],"poll-connected") || !strcmp(argv[1],"poll-connected-local"),initial=!strcmp(argv[1],"initial");
 if(initial){t.status=S_VSOCK;vsock.state=1;}
 local_listener=!strcmp(argv[1],"local") || !strcmp(argv[1],"connected-local") || !strcmp(argv[1],"poll-connected-local");
 if(!strcmp(argv[1],"poll-stream")){t.sk.sk_type=1;unsigned events=tsi_poll(NULL,&outer,NULL);return events==4 && t.status==S_VSOCK?0:1;}
 int unconnected=!strcmp(argv[1],"unconnected-nameless");
 if(unconnected)outer.state=0;
 for(int i=0;i<2;i++){
  struct sockaddr target={2,{0}};
  int nameless=connected || initial || (unconnected && i==1);
  struct msghdr msg={.msg_name=nameless?NULL:&target,.msg_namelen=nameless?0:16};
  if(!strcmp(argv[1],"poll-connected") || !strcmp(argv[1],"poll-connected-local")){__poll_t events=tsi_poll(NULL,&outer,NULL);printf("poll=%u status=%d\n",events,t.status);}
  int result=tsi_dgram_sendmsg(&outer,&msg,29);
  printf("send%d=%d status=%d\n",i+1,result,t.status);
  if(result!=(unconnected && i==1?-EINVAL:29))return 1;
 }
 if(local_listener && (inet_calls!=2 || vsock_calls!=0)){printf("wrong route inet=%d vsock=%d\n",inet_calls,vsock_calls);return 1;}
 return 0;
}
