"""Software-switch forwarding failure implementations."""

import shlex
import json

from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    capture_switch,
    restore_switch,
)
from nika.problems.support.polling import wait_until

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import choice, prefer_prefixed_node

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.oracle import (
    p4rt,
    OraclePlaybook,
    all_of,
    contains,
    lacks,
    shell,
)


class Bmv2SwitchDownParams(BaseModel):
    """Parameters for injecting a BMv2 switch down fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class Bmv2SwitchDown(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "path_http"
    symptom_class = "unreachable"
    root_cause_name = "bmv2_switch_down"
    description = "BMv2 switch dataplane process is down."
    TAGS: str = ["p4"]

    Params = Bmv2SwitchDownParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_fabric":
            return {
                "host_name": prefer_prefixed_node(
                    ctx.bmv2, prefix="leaf_", preferred="leaf_1", fallback="leaf_1"
                )
            }
        if ctx.scenario == "p4_dc_gateway":
            return {"host_name": ctx.net_env.model.clients[0].attached_switch}
        return {"host_name": choice(ctx.rng, ctx.bmv2, ctx.host0)}

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: Bmv2SwitchDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: Bmv2SwitchDownParams):
        self._original_switch = capture_switch(self.runtime, params.host_name)
        reader = (
            "import pathlib,json,shlex; rows=[]; "
            + "\nfor path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):\n try:\n  args=path.read_bytes().decode().strip('\\0').split('\\0')\n  if args and pathlib.Path(args[0]).name in ('simple_switch','simple_switch_grpc'): rows.append(shlex.join(args))\n except (OSError, UnicodeError): pass\nprint(json.dumps(rows))"
        )
        self._switch_commands = json.loads(
            self.runtime.exec(params.host_name, "python3 -c " + shlex.quote(reader))
        )
        if not self._switch_commands:
            raise RuntimeError(f"Cannot capture BMv2 command on {params.host_name}")
        # -f: match full cmdline (comm is truncated to 15 chars on Linux).
        self.runtime.exec(
            params.host_name,
            "pkill -9 -f '[s]imple_switch' 2>/dev/null || true",
        )

    def recover_fault(self, params: Bmv2SwitchDownParams) -> dict:
        commands = getattr(self, "_switch_commands", [])
        running = self.runtime.exec(
            params.host_name,
            "pgrep -af '[s]imple_switch' | grep -v '<defunct>' || true",
        ).strip()
        if commands and not running:
            for command in commands:
                self.runtime.exec(
                    params.host_name,
                    "nohup "
                    + shlex.join(shlex.split(command))
                    + " >>sw.log 2>&1 </dev/null &",
                )
            wait_until(
                lambda: bool(
                    self.runtime.exec(
                        params.host_name, "pgrep -f '[s]imple_switch' || true"
                    ).strip()
                ),
                10,
            )
        return restore_switch(
            self, params.host_name, getattr(self, "_original_switch", None)
        )

    def verify_fault(self, params: Bmv2SwitchDownParams) -> dict:
        """Verify the BMv2 dataplane process is not running (artifact gate)."""
        # Exclude zombies and exiting processes: after pkill -9 a <defunct>
        # entry, or one whose cmdline is already gone ("PID [comm]"), can
        # still match pgrep -af '[s]imple_switch'.
        pgrep_output = self.runtime.exec(
            params.host_name,
            "pgrep -af '[s]imple_switch' 2>/dev/null "
            "| grep -v -e '<defunct>' -e '^[0-9]* \\[' || echo NONE",
        ).strip()
        verified = pgrep_output == "NONE" or "simple_switch" not in pgrep_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )

    def oracle_playbook(self, params: Bmv2SwitchDownParams) -> OraclePlaybook:
        switch = params.host_name
        return OraclePlaybook(
            finding=(
                f"The BMv2 dataplane process on {switch} is gone: its links stay "
                "up but nothing forwards and its P4Runtime server does not answer."
            ),
            steps=[
                p4rt(
                    f"read --switch {switch}",
                    f"Query {switch}'s P4Runtime state through the controller.",
                    lacks(r'"ok":\s*true', f"{switch} does not answer P4Runtime"),
                ),
                shell(
                    switch,
                    "ip -br link | grep -E '^eth'",
                    f"Check the data-port link states on {switch}.",
                    all_of(
                        contains(r"^eth\S*\s+UP\b", "the data ports are up"),
                        lacks(r"^eth\S*\s+(DOWN|LOWERLAYERDOWN)", "no port is down"),
                    ),
                ),
                shell(
                    switch,
                    "pgrep -af '[s]imple_switch' "
                    "| grep -v -e '<defunct>' -e '^[0-9]* \\[' || echo NO_PROCESS",
                    f"Look for the BMv2 simple_switch process on {switch}.",
                    contains(r"^NO_PROCESS$", "no simple_switch process is running"),
                    evidence=True,
                ),
            ],
        )
