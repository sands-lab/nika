import json
from nika.utils.session import Session
from nika.problems.context import init_problem
s=Session().load_running_session(session_id='20261110-124329-50e107');e,r=init_problem(s.scenario_name,**s.scenario_params);a=r.lab_api
node='atlam5'
def capture():
 return {n:r.exec(n,"stat -c '%Y %Z' /etc/opt/srlinux/config.json; sha256sum /etc/opt/srlinux/config.json") for n in e.routers}
def show():return a.srl_exec_cli(node,'show interface ethernet-1/1')
print(json.dumps({'healthy':show(),'files':capture()}),flush=True)
try:
 a.srl_configure(node,'/ interface ethernet-1/1 admin-state disable')
 e.align_saved_configs()
 status=show();files=capture();saved=json.loads(r.exec(node,'cat /etc/opt/srlinux/config.json'))
 interface=next(i for i in saved['srl_nokia-interfaces:interface'] if i['name']=='ethernet-1/1')
 assert interface['admin-state']=='disable',interface
 assert 'ethernet-1/1 is down' in status,status
 assert len({x.split()[0] for x in files.values()})==1,files
 print(json.dumps({'faulted':status,'files':files,'saved_interface':interface}),flush=True)
finally:
 a.srl_configure(node,'/ interface ethernet-1/1 admin-state enable');e.align_saved_configs()
 status=show();assert 'ethernet-1/1 is up' in status,status
 print(json.dumps({'recovered':status,'files':capture()}),flush=True)
