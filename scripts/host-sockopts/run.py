#!/usr/bin/env python3
"""Verify socket options against packets and host reservations, not readback alone."""
import argparse, hashlib, json, os, pathlib, shutil, socket, subprocess, sys, tempfile, threading, traceback
p=argparse.ArgumentParser();p.add_argument('out',type=pathlib.Path);p.add_argument('--native',action='store_true');args=p.parse_args()
if args.native and sys.platform!='linux':p.error('native baseline requires Linux')
args.out.mkdir(parents=True,exist_ok=True);stop=threading.Event();sockets=[];threads=[];errors=[];config=[]
def echo(sock,level,types):
    try:
        while not stop.is_set():
            try:data,controls,flags,peer=sock.recvmsg(32,128)
            except socket.timeout:continue
            values=[int.from_bytes(value,sys.byteorder) for layer,kind,value in controls if layer==level and kind in types]
            assert len(values)==1,(controls,values)
            sock.sendto(values[0].to_bytes(4,'little'),peer)
    except Exception:errors.append(traceback.format_exc())
for label,family,ip in [('v4',socket.AF_INET,'127.0.0.1'),('v6',socket.AF_INET6,'::1'),('mapped-v4',socket.AF_INET,'127.0.0.1')]:
    sock=socket.socket(family,socket.SOCK_DGRAM);sockets.append(sock);sock.bind((ip,0));sock.settimeout(.1)
    if family==socket.AF_INET:
        level=socket.IPPROTO_IP;option=24 if sys.platform=='darwin' else 12;types=[socket.IP_TTL,option]
    else:level=socket.IPPROTO_IPV6;option=socket.IPV6_RECVHOPLIMIT;types=[socket.IPV6_HOPLIMIT]
    sock.setsockopt(level,option,1)
    thread=threading.Thread(target=echo,args=(sock,level,types));threads.append(thread);thread.start()
    peer=('[%s]:%d' if family==socket.AF_INET6 else '%s:%d')%(ip,sock.getsockname()[1])
    if label=='mapped-v4':peer='[::ffff:127.0.0.1]:%d'%sock.getsockname()[1]
    kinds=['reuse','shared-connected','shared-reuseport']
    if label!='mapped-v4':kinds=['ttl-prebind','ttl-bound','ttl-connected','ttl-invalid','ttl-disconnect']+kinds+(['broadcast'] if family==socket.AF_INET else ['v6only-bound'])
    for kind in kinds:config.append(label+'-'+kind+' '+kind+' '+peer)
def tcp_echo(sock):
    try:
        while not stop.is_set():
            try:conn,_=sock.accept()
            except socket.timeout:continue
            with conn:
                conn.settimeout(2)
                data=b''
                while len(data)<65536:
                    chunk=conn.recv(65536-len(data))
                    if not chunk:break
                    data+=chunk
                if data:conn.sendall(data)
    except Exception:errors.append(traceback.format_exc())
for label,family,ip in [('v4',socket.AF_INET,'127.0.0.1'),('v6',socket.AF_INET6,'::1')]:
    sock=socket.socket(family,socket.SOCK_STREAM);sockets.append(sock);sock.bind((ip,0));sock.listen();sock.settimeout(.1)
    thread=threading.Thread(target=tcp_echo,args=(sock,));threads.append(thread);thread.start()
    peer=('[%s]:%d' if family==socket.AF_INET6 else '%s:%d')%(ip,sock.getsockname()[1])
    for kind in ['tcp-connected','tcp-preconnect','tcp-accepted','tcp-accepted-inherited']:config.append(label+'-'+kind+' '+kind+' '+peer)
config.append('v4-ttl-query ttl-query 127.0.0.1:9')
config.append('shared-mixed shared-mixed 127.0.0.1:9')
if os.environ.get('SMOL_SOCKOPT_CASE'):
    config=[line for line in config if line.split()[0]==os.environ['SMOL_SOCKOPT_CASE']]
assert config
try:
    with tempfile.TemporaryDirectory(prefix='smol-sockopts-') as tmp:
        root=pathlib.Path(tmp);shutil.copy2(os.environ['SMOL_UDP_PROBE'],root/'probe');(root/'probe').chmod(0o755);(root/'sockopt-config').write_text('\n'.join(config)+'\n')
        env=os.environ.copy();env.update(SMOL_HOST_MODE='host',SMOL_DNS_FORWARDER='127.0.0.1:9');env.pop('SMOL_TCP_INTERCEPT',None)
        if args.native:command=[str(root/'probe')]
        else:
            library=pathlib.Path(env['SMOL_UDP_LIBRARY']).resolve();search=os.pathsep.join([str(library.parent),env.get('SMOL_UDP_LIB_DIR','')]);env.update(LD_LIBRARY_PATH=search,DYLD_LIBRARY_PATH=search,DYLD_FALLBACK_LIBRARY_PATH='/Library/Developer/CommandLineTools/usr/lib')
            command=[env['SMOL_UDP_LAUNCHER'],str(root),env['SMOL_UDP_KERNEL'],str(library)]
        try:
            with (args.out/'run.log').open('w') as log:run=subprocess.run(command,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=40);code=run.returncode
        except subprocess.TimeoutExpired:code=-1
finally:
    stop.set()
    for t in threads:t.join(timeout=2)
    for s in sockets:s.close()
log=(args.out/'run.log').read_text();cases=[{'name':f[1],'passed':f[2]=='PASS'} for line in log.splitlines() if line.startswith('SOCKOPT_CASE ') for f in [line.split()]]
loaded=[line[len('SMOL_RUNTIME_LIBRARY '):] for line in log.splitlines() if line.startswith('SMOL_RUNTIME_LIBRARY ')]
verified=args.native or (len(loaded)==1 and pathlib.Path(loaded[0]).resolve()==library)
result={'passed':code==0 and verified and not errors and [c['name'] for c in cases]==[c.split()[0] for c in config] and all(c['passed'] for c in cases),'exit':code,'cases':cases,'errors':errors,'runtime_verified':verified,'hashes':{k:hashlib.sha256(pathlib.Path(env[k]).read_bytes()).hexdigest() for k in ['SMOL_UDP_PROBE','SMOL_UDP_LIBRARY','SMOL_UDP_KERNEL'] if k in env}}
(args.out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2));sys.exit(0 if result['passed'] else 1)
