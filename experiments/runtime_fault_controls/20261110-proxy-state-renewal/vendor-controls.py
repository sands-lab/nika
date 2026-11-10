import json
from nika.utils.session import Session
from nika.problems.context import init_problem

def check(sid,kind):
 s=Session().load_running_session(session_id=sid);e,r=init_problem(s.scenario_name,**s.scenario_params);a=r.lab_api
 if kind=='routeros':
  from nika.service.kathara.routeros_api import KatharaRouterOSAPI
  a=KatharaRouterOSAPI(r.lab_name)
  paths='/ftpboot/config.auto.rsc'; change=lambda:a.routeros_exec('router1','/interface ethernet set ether2 comment=review-uplink');restore=lambda:a.routeros_exec('router1','/interface ethernet set ether2 comment=""'); live=lambda:a.routeros_exec('router1','/interface ethernet print detail where name=ether2')
 else:
  paths='/xr-storage/scratch/startup-config.cfg /etc/cisco/platform_boot.cfg'; change=lambda:a.iosxr_apply_config('router1',['interface GigabitEthernet0/0/0/0','description review-uplink','end']);restore=lambda:a.iosxr_apply_config('router1',['interface GigabitEthernet0/0/0/0','no description','end']);live=lambda:a.iosxr_exec('router1','show running-config interface GigabitEthernet0/0/0/0')
 read=lambda:{n:r.exec(n,f"stat -c '%n %Y %Z' {paths}; sha256sum {paths}") for n in e.routers}
 before=read(); print(json.dumps({'kind':kind,'before':before}),flush=True)
 try:
  print(json.dumps({'change':change(),'live':live()}),flush=True)
  assert 'review-uplink' in live()
  after=read(); print(json.dumps({'kind':kind,'after':after,'unchanged':before==after}),flush=True)
 finally:
  print(json.dumps({'restore':restore(),'live':live()}),flush=True)
check('20261110-124554-68ed62','routeros')
check('20261110-125156-b9f88e','iosxr')
