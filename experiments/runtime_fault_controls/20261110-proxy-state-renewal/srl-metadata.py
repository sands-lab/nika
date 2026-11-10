import json,hashlib
from nika.utils.session import Session
from nika.problems.context import init_problem
s=Session().load_running_session(session_id='20261110-124329-50e107')
e,r=init_problem(s.scenario_name,**s.scenario_params)
api=r.lab_api
read=lambda node:r.exec(node,"stat -c '%Y %Z' /etc/opt/srlinux/config.json; sha256sum /etc/opt/srlinux/config.json")
print(json.dumps({'before':{n:read(n) for n in e.routers}}),flush=True)
try:
 api.srl_configure('atlam5','/ interface ethernet-1/1 description review-uplink')
 e.align_saved_configs()
 after={n:read(n) for n in e.routers}
 print(json.dumps({'after':after}),flush=True)
 assert len({v.split()[0] for v in after.values()})==1
 assert int(r.exec('atlam5',"grep -c review-uplink /etc/opt/srlinux/config.json").strip())>=1
 assert all('.new' not in r.exec(n,'ls /etc/opt/srlinux/config.json*') for n in e.routers)
finally:
 api.srl_configure('atlam5','delete / interface ethernet-1/1 description')
 e.align_saved_configs()
print(json.dumps({'restored':{n:read(n) for n in e.routers}}),flush=True)
