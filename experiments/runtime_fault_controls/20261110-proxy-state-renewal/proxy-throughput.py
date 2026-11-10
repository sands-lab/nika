import json,time
from nika.mcp.session_context import get_lab_runtime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
r=get_lab_runtime(); p=KatharaVdeFaultProxy(r); state=None
src='pc_1_1_1_1';dst='pc_2_1_1_1';ip=r.get_data_plane_host_ip(dst)
def sample(stage,reverse=False):
 r.exec(dst,'timeout 20 iperf3 -s -1 -p 42001 >/dev/null 2>&1 &')
 for _ in range(4):
  out=r.exec(src,f'iperf3 -c {ip} -p 42001 -t 3 -J'+(' -R' if reverse else ''),timeout=15)
  data=json.loads(out)
  if 'error' not in data:break
  time.sleep(.2)
 print(json.dumps({'stage':stage,'reverse':reverse,'result':data}),flush=True)
 assert 'error' not in data,data
try:
 for reverse in (False,True):sample('native',reverse)
 state=p.insert(src,'eth0')
 for reverse in (False,True):sample('proxy-sham',reverse)
 p.set_netem_loss(state,5)
 for reverse in (False,True):sample('proxy-loss',reverse)
 p.remove(state);state=None
 for reverse in (False,True):sample('recovered',reverse)
finally:
 if state:p.remove(state)
