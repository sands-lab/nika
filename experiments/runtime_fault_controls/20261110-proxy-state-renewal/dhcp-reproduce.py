import json
from nika.utils.session import Session
from nika.workflows.failure.inject import prepare_failure
s=Session().load_running_session(session_id='20261110-123946-5c2bed')
f=prepare_failure(s,['host_incorrect_ip'],dict(host_name='pc_1_1_1_1',incorrect_ip='192.0.2.77/24'))
r=f.problem.runtime
read=lambda:r.exec('pc_1_1_1_1','ip -4 -o addr show dev eth0; ip -4 route')
print(json.dumps({'baseline':read()}),flush=True)
try:
 f.inject();print(json.dumps({'injected':read(),'verify':f.verify()}),flush=True)
 r.renew_dhcp_leases(['pc_1_1_1_1'])
 print(json.dumps({'renewed':read(),'verify':f.verify()}),flush=True)
finally:
 print(json.dumps({'recovered':f.recover(),'state':read()}),flush=True)
