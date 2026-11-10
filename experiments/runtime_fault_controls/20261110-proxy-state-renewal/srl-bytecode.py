import json
from nika.utils.session_store import SessionStore
from nika.utils.session import Session
from nika.problems.context import init_problem
rows=[s for s in SessionStore().list_running_sessions() if s.get('scenario_name')=='min3clos' and 'test' in s.get('session_id','')]
assert len(rows)==1,rows
s=Session().load_running_session(session_id=rows[0]['session_id']);e,r=init_problem(s.scenario_name,**s.scenario_params);node=e.routers[0]
command="find /opt/srlinux -type f -name '*.pyc' -printf '%p %T@ %C@\\n' | sort"
before=r.exec(node,command)
output=r.lab_api.srl_exec_cli(node,'show version')
after=r.exec(node,command)
print(json.dumps({'session_id':s.session_id,'node':node,'cache_files':len(before.splitlines()),'unchanged':before==after,'output':output}),flush=True)
assert before==after
