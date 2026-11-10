import json,os,time
from nika.mcp.session_context import get_lab_runtime
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.runtime.shared.processes import process_source
p=KatharaVdeFaultProxy(get_lab_runtime());s=p.insert('router_core_1','eth0');h=p._client.containers.get(s.proxy_id)
try:
 record=json.loads(h.exec_run(['cat','/run/splice.ready']).output);print(json.dumps(record),flush=True)
 code=process_source()+'''\nimport time\nr=read_identity('/run/splice.ready')
f=owned_pidfd(r['relay']);send_pidfd(f,signal.SIGKILL);os.close(f)
time.sleep(.2)
for k in ('relay','native'):
 pid=r[k][0]
 try:
  print(k,Path(f'/proc/{pid}/status').read_text().splitlines()[:9])
  print('identity',process_identity(pid))
  fd=owned_pidfd(r[k]);print('pidfd',fd)
  if fd is not None:os.close(fd)
 except Exception as e:print(type(e).__name__,str(e))
'''
 result=h.exec_run(['python3','-c', code]);print(result.output.decode(),flush=True)
finally:
 p.remove(s,suppress_errors=True)
