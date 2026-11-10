/* Intent: docs/tsi-udp-routing.md. Bounded socket/route seams. */
/* Root-authorized source correctness controls; INTENT.md. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define SOCK_DGRAM 2
#define PF_INET 2
#define AF_INET 2
#define PF_INET6 10
#define AF_INET6 10
#define WARN_ON_ONCE(x) (x)
typedef uint32_t __be32;
typedef uint16_t __be16;
struct in6_addr {uint32_t words[4];};
struct net {struct {void *udp_table;} ipv4;};
struct sock {int sk_type; int sk_bound_dev_if; unsigned sk_mark; int sk_protocol; struct in6_addr sk_v6_rcv_saddr; int refs;};
struct inet_sock {struct sock sk;__be32 inet_saddr;__be16 inet_sport;unsigned char tos;int uc_index;};
struct socket {struct sock *sk;};
struct tsi_sock {struct sock sk;int family;struct socket *isocket;};
struct sockaddr {uint16_t family;};
struct sockaddr_in {uint16_t sin_family,sin_port;struct {uint32_t s_addr;} sin_addr;char pad[8];};
struct sockaddr_in6 {uint16_t sin6_family,sin6_port;uint32_t flow;struct in6_addr sin6_addr;uint32_t scope;};
static struct net ns;
static struct sock listener={.refs=3};
static int held,unprotected,connected,missing;
static int wildcard,route_error,route_calls,route_refs,route_bad,alternate_interface;
static __be32 selected_source=0x0100007f;
struct flowi4 {__be32 saddr;};
struct rtable {int dummy;};
static struct rtable route;
#define IS_ERR(p) ((p)==(void *)-1)
#define RT_TOS(t) ((t)&0x1e)
#define READ_ONCE(x) (x)
#define pr_warn_ratelimited(...) (++warnings)
static int warnings;
#define PTR_ERR(p) ((void)(p),-1)
#define ip_rt_put(p) ((void)(p),--route_refs)
struct rtable *ip_route_output_ports(struct net *n,struct flowi4 *fl,const struct sock *sk,__be32 da,__be32 sa,__be16 dp,__be16 sp,unsigned char proto,unsigned char tos,int oif){
 (void)n;route_calls++;if(da!=0x0100007f || sa!=0 || dp!=23456 || sp!=12345 || proto!=17 || tos!=0x10 || oif!=(alternate_interface?9:7) || sk->sk_mark!=99)route_bad++;
 if(route_error)return (void *)-1;
 route_refs++;fl->saddr=selected_source;return &route;
}

#define tsi_sk(s) ((struct tsi_sock *)(s))
#define sock_net(s) ((void)(s),&ns)
#define inet_sk(s) ((struct inet_sock *)(s))
#define rcu_read_lock() (++held)
#define rcu_read_unlock() (--held)
#define sock_put(s) ((s)->refs--)
static struct sock *__udp4_lib_lookup(struct net *n,__be32 sa,__be16 sp,__be32 da,__be16 dp,int a,int b,void *t,void *skb){
 (void)n;(void)da;(void)dp;(void)a;(void)b;(void)t;(void)skb;
 if(!held)unprotected++;
 if(missing)return NULL;
 return connected && (sa!=selected_source || sp!=12345)?NULL:&listener;
}
static struct sock *__udp6_lib_lookup(struct net *n,const struct in6_addr *sa,__be16 sp,const struct in6_addr *da,__be16 dp,int a,int b,void *t,void *skb){
 (void)n;(void)da;(void)dp;(void)a;(void)b;(void)t;(void)skb;
 if(!held)unprotected++;
 if(missing)return NULL;
 return connected && (!sa || sa->words[3]!=1 || sp!=12345)?NULL:&listener;
}
#include "listener-under-test.c"
int main(int argc,char **argv){
 if(argc!=2)return 2;
 struct inet_sock inet={.sk={.sk_type=SOCK_DGRAM,.sk_bound_dev_if=7,.sk_mark=99,.sk_protocol=17,.sk_v6_rcv_saddr={{0,0,0,1}}},.inet_saddr=0x0100007f,.inet_sport=12345,.tos=0x10,.uc_index=9};
 struct socket shadow={&inet.sk};struct tsi_sock t={.sk={.sk_type=SOCK_DGRAM},.family=PF_INET,.isocket=&shadow};
 wildcard=strstr(argv[1],"wildcard")!=NULL;route_error=strstr(argv[1],"route-error")!=NULL;
 if(wildcard)inet.inet_saddr=0;
 if(strstr(argv[1],"uc-index")){inet.sk.sk_bound_dev_if=0;alternate_interface=1;}
 if(strstr(argv[1],"alternate-source"))selected_source=0x0200007f;
 connected=strstr(argv[1],"connected")!=NULL;
 bool v6=strstr(argv[1],"v6")!=NULL;
 struct sockaddr_in target={.sin_family=AF_INET,.sin_port=23456,.sin_addr={0x0100007f}};
 struct sockaddr_in6 target6={.sin6_family=AF_INET6,.sin6_port=23456,.sin6_addr={{0,0,0,1}}};
 if(v6)t.family=PF_INET6;
 missing=strstr(argv[1],"missing")!=NULL;
 bool invalid=!strcmp(argv[1],"invalid-family"),shortaddr=!strcmp(argv[1],"short-address"),nulladdr=!strcmp(argv[1],"null-address");
 if(invalid)target.sin_family=99;
 bool expected=!(missing||invalid||shortaddr||nulladdr||route_error);

 bool found=tsi_has_udp_listener(&t.sk,(struct sockaddr *)(nulladdr?NULL:(v6?(void *)&target6:(void *)&target)),shortaddr?1:(v6?sizeof(target6):sizeof(target)));
 printf("found=%d refs=%d unprotected=%d held=%d\n",found,listener.refs,unprotected,held);
 printf("routecalls=%d refs=%d bad=%d\n",route_calls,route_refs,route_bad);
 return found==expected && listener.refs==3 && !unprotected && !held && route_calls==(wildcard?1:0) && !route_refs && !route_bad && warnings==(route_error?1:0) ?0:1;
}
