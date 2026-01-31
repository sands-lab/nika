# NIKA Agent Architecture Analysis
## Adapting for Wireless Telecom/Cellular Networks

---

## 1. Overview of the Current Architecture

NIKA (Network Arena for AI) is a benchmarking framework where AI agents troubleshoot network issues using a **ReAct (Reasoning + Acting)** pattern. The architecture follows a modular, multi-agent design with clear separation of concerns.

### Core Components

```
┌──────────────────────────────────────────────────────────────────────┐
│                        BasicReActAgent                                │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │                    LangGraph StateGraph                         │  │
│  │                                                                 │  │
│  │   START ──► DiagnosisAgent ──► (if done) ──► SubmissionAgent ──► END │
│  │                  │                               │              │  │
│  │                  ▼                               ▼              │  │
│  │           MCP Servers                      Task MCP Server      │  │
│  │     (kathara_base, frr, bmv2, telemetry)    (submit tool)       │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                        ┌───────────────────────┐
                        │   Network Environment  │
                        │   (Kathara Containers) │
                        └───────────────────────┘
```

---

## 2. Key Architectural Patterns

### 2.1 Multi-Agent Workflow (LangGraph)

**File:** `src/agent/react_agent.py`

The orchestration uses LangGraph's `StateGraph` to manage agent state and workflow:

```python
class AgentState(TypedDict):
    messages: list[BaseMessage]      # Conversation history
    diagnosis_report: str            # Analysis output
    is_max_steps_reached: bool       # Control flag

# Workflow: START → diagnosis_agent → submission_agent → END
worker_builder = StateGraph(AgentState)
worker_builder.add_node("diagnosis_agent", self.diagnosis_agent_builder)
worker_builder.add_node("submission_agent", self.submission_agent_builder)
worker_builder.add_edge(START, "diagnosis_agent")
worker_builder.add_conditional_edges(
    "diagnosis_agent",
    lambda state: state.get("is_max_steps_reached", False),
    {True: END, False: "submission_agent"}
)
```

### 2.2 Specialized Sub-Agents

**DiagnosisAgent** (`src/agent/domain_agents/diagnosis_agent.py`):
- Expert system prompt for network troubleshooting
- Loads tools dynamically from MCP servers
- Focuses on: anomaly detection → fault localization → root cause analysis

**SubmissionAgent** (`src/agent/domain_agents/submission_agent.py`):
- Converts diagnosis findings into structured output
- Calls `submit()` tool with standardized format

### 2.3 Tool Exposure via MCP (Model Context Protocol)

**File:** `src/agent/utils/mcp_servers.py`

Tools are exposed via MCP servers that run as subprocesses:

```python
config = {
    "kathara_base_mcp_server": {
        "command": "python3",
        "args": ["kathara_base_mcp_server.py"],
        "transport": "stdio",
    },
    # Additional servers for FRR, BMV2, telemetry...
}
```

**Available Tools (kathara_base):**
| Tool | Purpose |
|------|---------|
| `get_reachability()` | Ping all host pairs |
| `ping_pair(host_a, host_b)` | Targeted connectivity test |
| `get_host_net_config(host)` | IP config, routing table |
| `systemctl_ops()` | Service management |
| `iperf_test()` | Bandwidth testing |
| `exec_shell()` | Generic command execution |

### 2.4 Environment Abstraction

**File:** `src/nika/net_env/base.py`

The `NetworkEnvBase` class provides a uniform interface:

```python
class NetworkEnvBase:
    def deploy(self): ...       # Start the lab
    def undeploy(self): ...     # Tear down
    def load_machines(self): ... # Categorize nodes (hosts, routers, switches)
    def get_topology(self): ... # Return link connections
    def get_info(self): ...     # Generate network description
```

### 2.5 Fault Injection System

Problems are categorized and injected systematically:

```python
class RootCauseCategory(StrEnum):
    LINK_FAILURE = "link_failure"
    END_HOST_FAILURE = "end_host_failure"
    NETWORK_NODE_ERROR = "network_node_error"
    RESOURCE_CONTENTION = "resource_contention"
    MISCONFIGURATION = "misconfiguration"
    NETWORK_UNDER_ATTACK = "network_under_attack"
    MULTIPLE_FAULTS = "multiple_faults"
```

---

## 3. Adapting for Wireless Telecom/Cellular Networks

### 3.1 Component Mapping

| NIKA Component | Cellular Network Equivalent |
|----------------|----------------------------|
| Kathara containers | Network simulators (ns-3, UERANSIM, Open5GS) |
| Routers (FRR) | gNodeB (5G base stations), Core network functions |
| Switches | UPF (User Plane Function), switches in transport |
| Hosts | UEs (User Equipment), IoT devices |
| Links | Radio links (Uu interface), backhaul/fronthaul |
| MCP servers | Telecom-specific tool servers |

### 3.2 Expanded Hierarchical Multi-Agent Architecture

The architecture uses an **Orchestrator-Expert pattern** where a central orchestrator agent performs initial triage, delegates to specialized domain experts, and compiles the final diagnosis.

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           CELLULAR TROUBLESHOOTING SYSTEM                               │
│  ┌───────────────────────────────────────────────────────────────────────────────────┐  │
│  │                              LangGraph StateGraph                                  │  │
│  │                                                                                    │  │
│  │   ┌─────────────────────────────────────────────────────────────────────────┐     │  │
│  │   │                      ORCHESTRATOR AGENT                                  │     │  │
│  │   │  • Initial triage & symptom analysis                                    │     │  │
│  │   │  • Expert delegation decisions                                          │     │  │
│  │   │  • Cross-domain correlation                                             │     │  │
│  │   │  • Final report compilation                                             │     │  │
│  │   └──────────────────────────────┬──────────────────────────────────────────┘     │  │
│  │                                  │                                                 │  │
│  │            ┌─────────────────────┼─────────────────────┐                          │  │
│  │            │                     │                     │                          │  │
│  │            ▼                     ▼                     ▼                          │  │
│  │   ┌───────────────┐    ┌────────────────┐    ┌─────────────────┐                 │  │
│  │   │  RAN EXPERT   │    │  CORE EXPERT   │    │TRANSPORT EXPERT │                 │  │
│  │   │    AGENT      │    │     AGENT      │    │     AGENT       │                 │  │
│  │   └───────────────┘    └────────────────┘    └─────────────────┘                 │  │
│  │            │                     │                     │                          │  │
│  │            │    ┌────────────────┴────────────────┐    │                          │  │
│  │            │    │                                 │    │                          │  │
│  │            ▼    ▼                                 ▼    ▼                          │  │
│  │   ┌───────────────┐                      ┌─────────────────┐                     │  │
│  │   │SECURITY EXPERT│                      │  QoS/PERF EXPERT│                     │  │
│  │   │    AGENT      │                      │      AGENT      │                     │  │
│  │   └───────────────┘                      └─────────────────┘                     │  │
│  │            │                                       │                              │  │
│  │            └───────────────────┬───────────────────┘                              │  │
│  │                                │                                                  │  │
│  │                                ▼                                                  │  │
│  │              ┌─────────────────────────────────────┐                             │  │
│  │              │    ORCHESTRATOR COMPILATION         │                             │  │
│  │              │  (Aggregates expert findings)       │                             │  │
│  │              └─────────────────────────────────────┘                             │  │
│  │                                │                                                  │  │
│  │                                ▼                                                  │  │
│  │              ┌─────────────────────────────────────┐                             │  │
│  │              │       SUBMISSION AGENT              │                             │  │
│  │              │  (Structured output generation)     │                             │  │
│  │              └─────────────────────────────────────┘                             │  │
│  └───────────────────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────────────────┘
                                          │
        ┌─────────────┬─────────────┬─────┴─────┬─────────────┬─────────────┐
        ▼             ▼             ▼           ▼             ▼             ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
  │ RAN MCP  │ │ Core MCP │ │Transport │ │ Security │ │Telemetry │ │  Alarm   │
  │  Server  │ │  Server  │ │MCP Server│ │MCP Server│ │MCP Server│ │MCP Server│
  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
```

#### Agent Roles and Responsibilities

| Agent | Role | Key Responsibilities |
|-------|------|---------------------|
| **Orchestrator** | Coordinator & Decision Maker | Initial triage, expert selection, cross-domain correlation, final compilation |
| **RAN Expert** | Radio Access Specialist | RF issues, interference, coverage, handovers, beam management |
| **Core Expert** | Core Network Specialist | AMF/SMF/UPF, sessions, mobility, authentication |
| **Transport Expert** | Backhaul/Fronthaul Specialist | F1/E1/Xn, eCPRI, IP transport, timing |
| **Security Expert** | Security Analyst | Rogue base stations, signaling attacks, authentication failures |
| **QoS/Performance Expert** | Performance Analyst | KPIs, SLA compliance, capacity, latency analysis |

---

## 4. Orchestrator Agent Implementation

### 4.1 Orchestrator State Definition

```python
# src/cellular_agent/orchestrator_agent.py
from typing import TypedDict, List, Optional, Literal
from langgraph.graph import StateGraph, START, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

class ExpertReport(TypedDict):
    """Report from an expert agent"""
    expert_name: str
    domain: str
    findings: str
    confidence: float  # 0.0 to 1.0
    suspected_root_causes: List[str]
    evidence: List[str]
    recommended_actions: List[str]

class OrchestratorState(TypedDict):
    """State shared across the orchestrator workflow"""
    # Input
    messages: List[BaseMessage]
    task_description: str
    network_context: str

    # Triage results
    initial_assessment: str
    symptom_domains: List[str]  # ["ran", "core", "transport", "security", "qos"]
    severity: Literal["critical", "major", "minor", "warning"]

    # Expert delegation
    experts_to_invoke: List[str]
    expert_reports: List[ExpertReport]

    # Final compilation
    cross_domain_correlations: List[str]
    final_diagnosis: str
    root_cause_chain: List[str]  # Causal chain of events
    confidence_score: float

    # Control
    current_phase: Literal["triage", "expert_analysis", "compilation", "submission"]
    iteration_count: int
    max_iterations: int
    needs_deeper_analysis: bool
```

### 4.2 Orchestrator Agent Core Logic

```python
class CellularOrchestratorAgent:
    """
    Master orchestrator that coordinates expert agents for cellular network troubleshooting.

    Workflow:
    1. TRIAGE: Analyze symptoms, determine severity, identify affected domains
    2. DELEGATE: Route to appropriate expert agents based on triage
    3. ANALYZE: Expert agents perform deep-dive analysis
    4. CORRELATE: Cross-reference findings across domains
    5. COMPILE: Generate final diagnosis with root cause chain
    """

    def __init__(
        self,
        backend_model: str = "gpt-4o",
        max_iterations: int = 3,
        parallel_experts: bool = True
    ):
        self.backend_model = backend_model
        self.max_iterations = max_iterations
        self.parallel_experts = parallel_experts

        # Initialize expert agents
        self.experts = {
            "ran": RANExpertAgent(backend_model),
            "core": CoreExpertAgent(backend_model),
            "transport": TransportExpertAgent(backend_model),
            "security": SecurityExpertAgent(backend_model),
            "qos": QoSExpertAgent(backend_model),
        }

        # Build the workflow graph
        self.workflow = self._build_workflow()

    def _build_workflow(self) -> StateGraph:
        """Build the LangGraph workflow for orchestration"""
        builder = StateGraph(OrchestratorState)

        # Add nodes
        builder.add_node("triage", self._triage_node)
        builder.add_node("delegate_experts", self._delegate_experts_node)
        builder.add_node("run_ran_expert", self._run_ran_expert)
        builder.add_node("run_core_expert", self._run_core_expert)
        builder.add_node("run_transport_expert", self._run_transport_expert)
        builder.add_node("run_security_expert", self._run_security_expert)
        builder.add_node("run_qos_expert", self._run_qos_expert)
        builder.add_node("correlate_findings", self._correlate_findings_node)
        builder.add_node("compile_diagnosis", self._compile_diagnosis_node)
        builder.add_node("check_completeness", self._check_completeness_node)
        builder.add_node("submission", self._submission_node)

        # Define edges
        builder.add_edge(START, "triage")
        builder.add_edge("triage", "delegate_experts")

        # Conditional routing to experts based on triage
        builder.add_conditional_edges(
            "delegate_experts",
            self._route_to_experts,
            {
                "ran": "run_ran_expert",
                "core": "run_core_expert",
                "transport": "run_transport_expert",
                "security": "run_security_expert",
                "qos": "run_qos_expert",
                "correlate": "correlate_findings",
            }
        )

        # Expert completion edges
        for expert in ["ran", "core", "transport", "security", "qos"]:
            builder.add_edge(f"run_{expert}_expert", "correlate_findings")

        builder.add_edge("correlate_findings", "compile_diagnosis")
        builder.add_edge("compile_diagnosis", "check_completeness")

        # Check if we need another iteration
        builder.add_conditional_edges(
            "check_completeness",
            self._check_if_complete,
            {
                "complete": "submission",
                "needs_more": "delegate_experts",
            }
        )

        builder.add_edge("submission", END)

        return builder.compile()

    async def _triage_node(self, state: OrchestratorState) -> OrchestratorState:
        """
        Initial triage phase: Analyze symptoms and determine which experts to invoke.
        """
        triage_prompt = f"""
        You are the orchestrator for a cellular network troubleshooting system.

        TASK: Perform initial triage on the following network issue.

        Network Context:
        {state['network_context']}

        Problem Description:
        {state['task_description']}

        Analyze the symptoms and determine:
        1. SEVERITY: critical/major/minor/warning
        2. AFFECTED DOMAINS: Which network domains are potentially involved?
           - ran: Radio access issues (RF, coverage, handovers, interference)
           - core: Core network issues (AMF/SMF/UPF, sessions, authentication)
           - transport: Backhaul/fronthaul issues (F1/E1/Xn, IP transport, timing)
           - security: Security threats (rogue BS, signaling attacks, breaches)
           - qos: Performance degradation (KPI violations, SLA breaches, capacity)
        3. INITIAL ASSESSMENT: Brief hypothesis of what might be wrong

        Output as JSON:
        {{
            "severity": "critical|major|minor|warning",
            "symptom_domains": ["ran", "core", ...],
            "initial_assessment": "...",
            "primary_suspect_domain": "ran|core|transport|security|qos",
            "reasoning": "..."
        }}
        """

        response = await self.llm.ainvoke(triage_prompt)
        triage_result = parse_json_response(response)

        return {
            **state,
            "severity": triage_result["severity"],
            "symptom_domains": triage_result["symptom_domains"],
            "initial_assessment": triage_result["initial_assessment"],
            "experts_to_invoke": triage_result["symptom_domains"],
            "current_phase": "expert_analysis",
        }

    async def _delegate_experts_node(self, state: OrchestratorState) -> OrchestratorState:
        """
        Prepare context and instructions for expert agents.
        """
        expert_context = f"""
        ORCHESTRATOR TRIAGE SUMMARY:
        - Severity: {state['severity']}
        - Initial Assessment: {state['initial_assessment']}
        - Your domain was flagged for investigation

        Previous findings (if any):
        {self._format_previous_findings(state['expert_reports'])}

        INSTRUCTIONS:
        1. Perform deep-dive analysis in your domain
        2. Use available tools to gather evidence
        3. Report findings with confidence level
        4. Identify potential root causes in your domain
        5. Note any cross-domain dependencies you observe
        """

        return {
            **state,
            "expert_context": expert_context,
        }

    def _route_to_experts(self, state: OrchestratorState) -> str:
        """Determine which expert to invoke next"""
        pending_experts = [
            e for e in state["experts_to_invoke"]
            if e not in [r["domain"] for r in state.get("expert_reports", [])]
        ]

        if not pending_experts:
            return "correlate"

        return pending_experts[0]

    async def _correlate_findings_node(self, state: OrchestratorState) -> OrchestratorState:
        """
        Cross-correlate findings from all expert agents.
        """
        correlation_prompt = f"""
        You are analyzing expert reports to find cross-domain correlations.

        EXPERT REPORTS:
        {self._format_expert_reports(state['expert_reports'])}

        TASKS:
        1. Identify correlations between findings across domains
        2. Look for causal chains (e.g., transport issue → RAN degradation → UE disconnects)
        3. Identify contradictions or gaps in the analysis
        4. Determine if additional expert analysis is needed

        Output as JSON:
        {{
            "correlations": ["correlation 1", "correlation 2", ...],
            "causal_chain": ["event1 → event2 → event3"],
            "contradictions": ["..."],
            "gaps": ["..."],
            "needs_deeper_analysis": true/false,
            "additional_domains_to_check": ["domain1", ...]
        }}
        """

        response = await self.llm.ainvoke(correlation_prompt)
        correlation_result = parse_json_response(response)

        return {
            **state,
            "cross_domain_correlations": correlation_result["correlations"],
            "root_cause_chain": correlation_result["causal_chain"],
            "needs_deeper_analysis": correlation_result["needs_deeper_analysis"],
            "experts_to_invoke": correlation_result.get("additional_domains_to_check", []),
        }

    async def _compile_diagnosis_node(self, state: OrchestratorState) -> OrchestratorState:
        """
        Compile final diagnosis from all expert findings and correlations.
        """
        compilation_prompt = f"""
        You are the orchestrator compiling the final diagnosis.

        INITIAL TRIAGE:
        {state['initial_assessment']}

        EXPERT FINDINGS:
        {self._format_expert_reports(state['expert_reports'])}

        CROSS-DOMAIN CORRELATIONS:
        {state['cross_domain_correlations']}

        CAUSAL CHAIN:
        {state['root_cause_chain']}

        COMPILE THE FINAL DIAGNOSIS:
        1. Synthesize all findings into a coherent narrative
        2. Identify the PRIMARY root cause
        3. Identify CONTRIBUTING factors
        4. Calculate overall confidence score (0.0-1.0)
        5. Provide recommended remediation steps in priority order

        Output as JSON:
        {{
            "final_diagnosis": "...",
            "primary_root_cause": {{
                "category": "...",
                "description": "...",
                "affected_components": ["..."]
            }},
            "contributing_factors": ["..."],
            "confidence_score": 0.85,
            "remediation_steps": [
                {{"priority": 1, "action": "...", "domain": "..."}},
                ...
            ],
            "lessons_learned": ["..."]
        }}
        """

        response = await self.llm.ainvoke(compilation_prompt)
        diagnosis = parse_json_response(response)

        return {
            **state,
            "final_diagnosis": diagnosis["final_diagnosis"],
            "confidence_score": diagnosis["confidence_score"],
            "current_phase": "submission",
        }

    async def run(self, task_description: str, network_context: str) -> dict:
        """Execute the full orchestration workflow"""
        initial_state = OrchestratorState(
            messages=[HumanMessage(content=task_description)],
            task_description=task_description,
            network_context=network_context,
            initial_assessment="",
            symptom_domains=[],
            severity="warning",
            experts_to_invoke=[],
            expert_reports=[],
            cross_domain_correlations=[],
            final_diagnosis="",
            root_cause_chain=[],
            confidence_score=0.0,
            current_phase="triage",
            iteration_count=0,
            max_iterations=self.max_iterations,
            needs_deeper_analysis=False,
        )

        result = await self.workflow.ainvoke(initial_state)
        return result
```

### 4.3 Orchestrator System Prompt

```python
ORCHESTRATOR_SYSTEM_PROMPT = """
You are the MASTER ORCHESTRATOR for a cellular network troubleshooting system.

YOUR ROLE:
- You are the central coordinator overseeing all expert agents
- You perform initial triage to understand the problem scope
- You delegate to specialized experts based on symptom analysis
- You correlate findings across network domains
- You compile the final diagnosis and root cause analysis

TROUBLESHOOTING PHILOSOPHY:
1. Start broad, then narrow down (funnel approach)
2. Always consider cross-domain dependencies
3. Look for cascading failures (one issue causing others)
4. Validate hypotheses with evidence from experts
5. Maintain confidence scoring throughout

EXPERT AGENTS AVAILABLE:
- RAN Expert: Radio access network specialist (RF, coverage, handovers)
- Core Expert: Core network specialist (AMF/SMF/UPF, sessions)
- Transport Expert: Backhaul/fronthaul specialist (IP transport, timing)
- Security Expert: Security analyst (attacks, anomalies)
- QoS Expert: Performance analyst (KPIs, SLAs, capacity)

DECISION RULES FOR EXPERT DELEGATION:
- Call drop/poor signal → RAN Expert first
- Authentication failure → Core Expert first
- Latency issues → Transport Expert + QoS Expert
- Suspicious activity → Security Expert first
- Capacity problems → QoS Expert + RAN Expert

OUTPUT REQUIREMENTS:
- Provide clear reasoning for all decisions
- Always include confidence levels
- Identify the causal chain of events
- Recommend remediation in priority order
"""
```

---

## 5. Specialized Expert Agents

### 5.1 Expert Agent Base Class

```python
# src/cellular_agent/domain_agents/expert_base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any
from langchain_core.messages import BaseMessage
from mcp_use import MCPAgent

class ExpertAgentBase(ABC):
    """Base class for all expert agents"""

    def __init__(
        self,
        backend_model: str,
        max_steps: int = 15,
        mcp_servers: List[str] = None
    ):
        self.backend_model = backend_model
        self.max_steps = max_steps
        self.mcp_servers = mcp_servers or []
        self.system_prompt = self._get_system_prompt()
        self.agent = self._build_agent()

    @abstractmethod
    def _get_system_prompt(self) -> str:
        """Return the specialized system prompt for this expert"""
        pass

    @abstractmethod
    def _get_mcp_server_config(self) -> Dict[str, Any]:
        """Return MCP server configuration for this expert"""
        pass

    @property
    @abstractmethod
    def domain(self) -> str:
        """Return the domain this expert covers"""
        pass

    def _build_agent(self) -> MCPAgent:
        """Build the MCP-enabled agent"""
        from langchain_openai import ChatOpenAI

        llm = ChatOpenAI(model=self.backend_model, temperature=0)
        mcp_config = self._get_mcp_server_config()

        return MCPAgent(
            llm=llm,
            mcp_servers=mcp_config,
            max_steps=self.max_steps,
            system_prompt_template=self.system_prompt,
        )

    async def analyze(
        self,
        task_context: str,
        orchestrator_guidance: str,
        previous_findings: List[Dict] = None
    ) -> Dict:
        """
        Perform expert analysis on the given context.

        Returns:
            ExpertReport with findings, confidence, and recommendations
        """
        analysis_prompt = f"""
        ORCHESTRATOR GUIDANCE:
        {orchestrator_guidance}

        TASK CONTEXT:
        {task_context}

        PREVIOUS FINDINGS FROM OTHER EXPERTS:
        {self._format_previous_findings(previous_findings)}

        Perform deep analysis in your domain ({self.domain}).
        Use your tools to gather evidence before drawing conclusions.
        """

        result = await self.agent.ainvoke({"messages": [HumanMessage(content=analysis_prompt)]})

        return {
            "expert_name": self.__class__.__name__,
            "domain": self.domain,
            "findings": result.get("diagnosis_report", ""),
            "confidence": self._extract_confidence(result),
            "suspected_root_causes": self._extract_root_causes(result),
            "evidence": self._extract_evidence(result),
            "recommended_actions": self._extract_actions(result),
        }
```

### 5.2 RAN Expert Agent

```python
# src/cellular_agent/domain_agents/ran_expert_agent.py

class RANExpertAgent(ExpertAgentBase):
    """
    Radio Access Network Expert Agent

    Specializes in:
    - RF propagation and interference analysis
    - Coverage and capacity optimization
    - Handover and mobility management
    - Beamforming and MIMO performance
    - RRC state machine analysis
    """

    @property
    def domain(self) -> str:
        return "ran"

    def _get_mcp_server_config(self) -> Dict[str, Any]:
        return {
            "ran_mcp_server": {
                "command": "python3",
                "args": ["ran_mcp_server.py"],
                "transport": "stdio",
            },
            "telemetry_mcp_server": {
                "command": "python3",
                "args": ["telecom_telemetry_mcp_server.py"],
                "transport": "stdio",
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are the RAN EXPERT AGENT specializing in Radio Access Network troubleshooting for 4G/5G networks.

EXPERTISE AREAS:
1. RF Analysis
   - Signal propagation (path loss, fading, shadowing)
   - Interference detection (inter-cell, external, PIM)
   - RSRP/RSRQ/SINR interpretation
   - Antenna patterns and coverage

2. Capacity Analysis
   - PRB utilization and scheduling
   - Active UE counts and distribution
   - Throughput per cell/UE
   - Congestion patterns

3. Mobility Management
   - Handover success/failure analysis
   - Ping-pong detection
   - Inter-RAT handovers (5G↔4G)
   - Cell reselection issues

4. Beamforming (5G NR)
   - SSB beam patterns
   - CSI-RS measurements
   - Beam tracking and alignment
   - MIMO layer analysis

DIAGNOSTIC APPROACH:
1. Start with KPI trends (call drops, handover failures, throughput)
2. Correlate with RF measurements from affected UEs
3. Check cell-level metrics (PRB, active users, interference)
4. Analyze mobility events timeline
5. Look for spatial patterns (coverage holes, interference zones)

AVAILABLE TOOLS:
- get_gnb_metrics(gnb_id): Cell-level performance metrics
- get_ue_measurements(ue_id): UE RF measurements
- get_handover_history(ue_id, duration): Handover event log
- analyze_interference(cell_id): Inter-cell interference analysis
- get_rrc_state_transitions(ue_id): RRC state timeline
- check_beam_alignment(gnb_id, ue_id): Beam tracking status
- get_coverage_map(area_id): Coverage heatmap
- get_prb_utilization(cell_id, duration): Resource block usage

COMMON ROOT CAUSES IN RAN:
- Physical: Antenna damage, feeder cable issues, RRU failure
- RF: Interference (external/PIM), coverage gaps, overshooting
- Config: Wrong neighbor relations, incorrect handover parameters
- Capacity: PRB exhaustion, insufficient carriers
- Mobility: Aggressive/conservative handover thresholds, missing neighbors

OUTPUT FORMAT:
Report your findings with:
1. Observed symptoms and metrics
2. Evidence gathered from tools
3. Root cause hypothesis with confidence (0-1)
4. Cross-domain indicators (issues that may involve Core/Transport)
5. Recommended actions prioritized by impact
"""
```

### 5.3 Core Network Expert Agent

```python
# src/cellular_agent/domain_agents/core_expert_agent.py

class CoreExpertAgent(ExpertAgentBase):
    """
    Core Network Expert Agent

    Specializes in:
    - 5G Core (5GC) Network Functions: AMF, SMF, UPF, UDM, AUSF, PCF, NRF
    - Session management and PDU sessions
    - Mobility management and tracking areas
    - Authentication and security
    - Network slicing
    """

    @property
    def domain(self) -> str:
        return "core"

    def _get_mcp_server_config(self) -> Dict[str, Any]:
        return {
            "core_mcp_server": {
                "command": "python3",
                "args": ["core_mcp_server.py"],
                "transport": "stdio",
            },
            "telemetry_mcp_server": {
                "command": "python3",
                "args": ["telecom_telemetry_mcp_server.py"],
                "transport": "stdio",
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are the CORE NETWORK EXPERT AGENT specializing in 5G Core (5GC) troubleshooting.

EXPERTISE AREAS:
1. Access and Mobility Management (AMF)
   - Registration procedures (initial, mobility, periodic)
   - Tracking Area management
   - Connection management (CM-IDLE, CM-CONNECTED)
   - Paging procedures

2. Session Management (SMF/UPF)
   - PDU session establishment/modification/release
   - QoS flow management
   - UPF selection and N3/N9 tunnel setup
   - IP address allocation (IPv4/IPv6)

3. Subscriber Management (UDM/UDR)
   - Subscription data retrieval
   - Access authorization
   - Session continuity parameters

4. Authentication (AUSF/UDM)
   - 5G-AKA procedures
   - Authentication failures
   - Security context management

5. Policy Control (PCF)
   - Policy decisions for sessions
   - QoS policy enforcement
   - Charging control

6. Network Slicing
   - Slice selection (NSSF)
   - S-NSSAI management
   - Slice-specific routing

DIAGNOSTIC APPROACH:
1. Check NF health status (AMF, SMF, UPF, etc.)
2. Trace session establishment flows
3. Verify subscriber profile and authorization
4. Analyze authentication procedures
5. Check inter-NF communication (SBI interfaces)

AVAILABLE TOOLS:
- get_amf_status(): AMF health and connected gNB count
- get_smf_status(): SMF health and active sessions
- get_upf_status(): UPF health and tunnel count
- get_pdu_session_info(session_id): Session details
- get_subscriber_profile(supi): UDM subscription data
- check_authentication_status(ue_id): Auth context
- trace_registration_flow(ue_id): Registration procedure trace
- get_slice_stats(slice_id): Network slice metrics
- query_sbi_logs(nf_pair, duration): Service-based interface logs

COMMON ROOT CAUSES IN CORE:
- AMF: Registration failures, tracking area issues, paging failures
- SMF: PDU session failures, QoS mapping errors, UPF selection issues
- UPF: Tunnel failures, routing issues, N3/N9 problems
- Authentication: Credential mismatch, AUSF timeout, security failures
- Slicing: Wrong slice selection, slice capacity exhaustion

OUTPUT FORMAT:
Report your findings with:
1. NF health status summary
2. Procedure traces with failure points
3. Root cause hypothesis with confidence
4. Evidence from logs and metrics
5. Cross-domain indicators (RAN/Transport dependencies)
"""
```

### 5.4 Transport Expert Agent

```python
# src/cellular_agent/domain_agents/transport_expert_agent.py

class TransportExpertAgent(ExpertAgentBase):
    """
    Transport Network Expert Agent

    Specializes in:
    - Fronthaul (eCPRI, F1 interface)
    - Midhaul (F1 interface for CU-DU split)
    - Backhaul (N2/N3 to Core)
    - IP/MPLS transport network
    - Timing and synchronization
    """

    @property
    def domain(self) -> str:
        return "transport"

    def _get_mcp_server_config(self) -> Dict[str, Any]:
        return {
            "transport_mcp_server": {
                "command": "python3",
                "args": ["transport_mcp_server.py"],
                "transport": "stdio",
            },
            "telemetry_mcp_server": {
                "command": "python3",
                "args": ["telecom_telemetry_mcp_server.py"],
                "transport": "stdio",
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are the TRANSPORT NETWORK EXPERT AGENT specializing in mobile backhaul/fronthaul troubleshooting.

EXPERTISE AREAS:
1. Fronthaul (eCPRI/CPRI)
   - O-RAN fronthaul connectivity
   - eCPRI frame analysis
   - Timing over fronthaul
   - Bandwidth utilization

2. Midhaul (F1 Interface)
   - CU-DU connectivity
   - F1-C (control plane) health
   - F1-U (user plane) performance
   - SCTP association status

3. Backhaul (N2/N3/Xn)
   - gNB to Core connectivity
   - N2 (control plane to AMF)
   - N3 (user plane to UPF)
   - Xn interface (inter-gNB)

4. IP/MPLS Transport
   - Router/switch health
   - Link utilization and congestion
   - Routing protocol status (OSPF/BGP/IS-IS)
   - MPLS LSP status
   - QoS and traffic engineering

5. Timing and Synchronization
   - PTP (IEEE 1588) status
   - SyncE operation
   - GPS/GNSS receiver status
   - Timing accuracy and holdover

DIAGNOSTIC APPROACH:
1. Check transport link status and utilization
2. Verify end-to-end connectivity (ping, traceroute)
3. Analyze latency and jitter metrics
4. Check timing synchronization status
5. Review routing and MPLS state

AVAILABLE TOOLS:
- get_link_status(link_id): Link operational status
- get_link_utilization(link_id, duration): Bandwidth usage
- ping_transport_path(src, dst): ICMP reachability
- traceroute_path(src, dst): Path analysis
- get_latency_jitter(path_id, duration): Delay metrics
- get_ptp_status(node_id): PTP sync status
- get_routing_table(router_id): IP routes
- get_mpls_lsp_status(lsp_id): MPLS tunnel status
- get_interface_errors(interface_id): Error counters

COMMON ROOT CAUSES IN TRANSPORT:
- Physical: Fiber cuts, connector issues, hardware failures
- Congestion: Link saturation, queue drops, buffer overflow
- Routing: Route flaps, black holes, suboptimal paths
- Timing: PTP failures, GPS issues, clock drift
- Configuration: MTU mismatches, QoS misconfig, VLAN issues

OUTPUT FORMAT:
Report your findings with:
1. Transport path status summary
2. Performance metrics (latency, jitter, loss)
3. Timing synchronization status
4. Root cause hypothesis with confidence
5. Cross-domain impact (RAN/Core dependencies)
"""
```

### 5.5 Security Expert Agent

```python
# src/cellular_agent/domain_agents/security_expert_agent.py

class SecurityExpertAgent(ExpertAgentBase):
    """
    Security Expert Agent

    Specializes in:
    - Rogue base station detection
    - Signaling attacks (DoS, storms)
    - Authentication anomalies
    - Encryption and integrity verification
    - Subscriber privacy protection
    """

    @property
    def domain(self) -> str:
        return "security"

    def _get_mcp_server_config(self) -> Dict[str, Any]:
        return {
            "security_mcp_server": {
                "command": "python3",
                "args": ["security_mcp_server.py"],
                "transport": "stdio",
            },
            "alarm_mcp_server": {
                "command": "python3",
                "args": ["alarm_mcp_server.py"],
                "transport": "stdio",
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are the SECURITY EXPERT AGENT specializing in cellular network security analysis.

EXPERTISE AREAS:
1. Rogue Base Station Detection
   - Unauthorized cell identification
   - IMSI catcher detection
   - Cell ID anomalies
   - Unusual handover patterns

2. Signaling Security
   - NAS/RRC message analysis
   - Signaling storm detection
   - DoS attack identification
   - Protocol exploitation attempts

3. Authentication Security
   - Authentication failure patterns
   - Credential compromise indicators
   - Replay attack detection
   - SUPI/SUCI privacy violations

4. Encryption/Integrity
   - Cipher algorithm negotiation
   - NULL encryption detection
   - Integrity check failures
   - Key derivation issues

5. Subscriber Privacy
   - IMSI/SUPI exposure
   - Location tracking attempts
   - Unauthorized data access
   - Privacy policy violations

DIAGNOSTIC APPROACH:
1. Review security alarms and anomaly alerts
2. Analyze authentication failure patterns
3. Check for unusual signaling volumes
4. Verify encryption settings
5. Look for suspicious cell/UE behavior

AVAILABLE TOOLS:
- get_security_alarms(severity, duration): Security alerts
- detect_rogue_cells(area_id): Rogue BS detection
- analyze_auth_failures(ue_id, duration): Auth failure patterns
- get_signaling_stats(interface, duration): NAS/RRC volumes
- check_encryption_status(ue_id): Cipher/integrity status
- detect_signaling_storm(threshold): DoS detection
- get_imsi_exposure_events(duration): Privacy violations
- analyze_handover_anomalies(cell_id): Suspicious mobility

COMMON SECURITY THREATS:
- IMSI Catchers: Fake base stations capturing subscriber IDs
- Signaling DoS: Flooding NAS/RRC procedures
- Authentication Attacks: Credential theft, replay attacks
- Downgrade Attacks: Forcing weaker encryption
- Location Tracking: Unauthorized UE location monitoring

OUTPUT FORMAT:
Report your findings with:
1. Security alarm summary
2. Threat indicators detected
3. Affected subscribers/cells
4. Attack vector hypothesis
5. Recommended mitigations (immediate and long-term)
"""
```

### 5.6 QoS/Performance Expert Agent

```python
# src/cellular_agent/domain_agents/qos_expert_agent.py

class QoSExpertAgent(ExpertAgentBase):
    """
    QoS and Performance Expert Agent

    Specializes in:
    - KPI monitoring and analysis
    - SLA compliance verification
    - Capacity planning
    - Traffic pattern analysis
    - End-to-end latency analysis
    """

    @property
    def domain(self) -> str:
        return "qos"

    def _get_mcp_server_config(self) -> Dict[str, Any]:
        return {
            "telemetry_mcp_server": {
                "command": "python3",
                "args": ["telecom_telemetry_mcp_server.py"],
                "transport": "stdio",
            },
            "kpi_mcp_server": {
                "command": "python3",
                "args": ["kpi_mcp_server.py"],
                "transport": "stdio",
            },
        }

    def _get_system_prompt(self) -> str:
        return """
You are the QoS/PERFORMANCE EXPERT AGENT specializing in cellular network performance analysis.

EXPERTISE AREAS:
1. KPI Analysis
   - Accessibility KPIs (RRC/ERAB setup success rate)
   - Retainability KPIs (call drop rate, session continuity)
   - Mobility KPIs (handover success rate)
   - Integrity KPIs (throughput, latency, packet loss)
   - Availability KPIs (cell availability, NF uptime)

2. SLA Compliance
   - Latency SLA verification
   - Throughput guarantees
   - Availability targets
   - Slice-specific SLAs

3. Capacity Analysis
   - Traffic volume trends
   - Peak hour analysis
   - Resource utilization forecasting
   - Dimensioning validation

4. End-to-End Performance
   - User-perceived quality
   - Application-level metrics
   - OTT service performance
   - Gaming/video streaming QoE

5. Traffic Engineering
   - Load balancing effectiveness
   - Traffic steering policies
   - QoS flow prioritization
   - Congestion management

DIAGNOSTIC APPROACH:
1. Review KPI dashboards for anomalies
2. Compare against baselines and thresholds
3. Identify temporal patterns (time-of-day, day-of-week)
4. Correlate across network layers
5. Trace end-to-end performance path

AVAILABLE TOOLS:
- get_kpi_trends(kpi_name, scope, duration): KPI time series
- get_kpi_baseline(kpi_name, scope): Historical baseline
- check_sla_compliance(slice_id): SLA status
- get_traffic_volume(scope, duration): Traffic statistics
- analyze_peak_hours(scope, days): Peak analysis
- get_e2e_latency(src_ue, dst, duration): End-to-end delay
- get_throughput_stats(scope, duration): Throughput metrics
- get_packet_loss_stats(path, duration): Loss analysis
- forecast_capacity(scope, horizon): Capacity prediction

KEY PERFORMANCE INDICATORS:
- RRC Setup Success Rate: >99.5%
- ERAB Setup Success Rate: >99.0%
- Call Drop Rate: <1%
- Handover Success Rate: >98%
- User Throughput: >10 Mbps (DL), >5 Mbps (UL)
- Latency: <20ms (eMBB), <10ms (URLLC)
- Packet Loss: <0.1%

OUTPUT FORMAT:
Report your findings with:
1. KPI summary with deviations from baseline
2. SLA compliance status
3. Performance bottleneck identification
4. Capacity utilization assessment
5. Root cause hypothesis linking to other domains
"""
```

---

## 6. LangGraph Workflow Implementation

### 6.1 Complete Workflow Graph

```python
# src/cellular_agent/workflow.py
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

def build_cellular_troubleshooting_workflow():
    """
    Build the complete multi-agent workflow for cellular troubleshooting.

    Flow:
    ┌─────────────────────────────────────────────────────────────────┐
    │  START                                                          │
    │    │                                                            │
    │    ▼                                                            │
    │  TRIAGE (Orchestrator)                                          │
    │    │                                                            │
    │    ▼                                                            │
    │  DELEGATE ──────┬──────┬──────┬──────┬──────┐                  │
    │    │            │      │      │      │      │                   │
    │    ▼            ▼      ▼      ▼      ▼      ▼                   │
    │   RAN        Core  Transport Security  QoS                     │
    │  Expert     Expert  Expert   Expert   Expert                   │
    │    │            │      │      │      │      │                   │
    │    └────────────┴──────┴──────┴──────┴──────┘                   │
    │                         │                                       │
    │                         ▼                                       │
    │               CORRELATE (Orchestrator)                          │
    │                         │                                       │
    │                         ▼                                       │
    │                COMPILE (Orchestrator)                           │
    │                         │                                       │
    │           ┌─────────────┴─────────────┐                        │
    │           │                           │                        │
    │           ▼                           ▼                        │
    │    needs_more_analysis?          SUBMIT ──► END                │
    │           │                                                     │
    │           └──────► back to DELEGATE                            │
    └─────────────────────────────────────────────────────────────────┘
    """

    workflow = StateGraph(OrchestratorState)

    # === NODES ===

    # Orchestrator nodes
    workflow.add_node("triage", orchestrator_triage)
    workflow.add_node("delegate", orchestrator_delegate)
    workflow.add_node("correlate", orchestrator_correlate)
    workflow.add_node("compile", orchestrator_compile)
    workflow.add_node("submit", orchestrator_submit)

    # Expert nodes (can run in parallel)
    workflow.add_node("ran_expert", run_ran_expert)
    workflow.add_node("core_expert", run_core_expert)
    workflow.add_node("transport_expert", run_transport_expert)
    workflow.add_node("security_expert", run_security_expert)
    workflow.add_node("qos_expert", run_qos_expert)

    # Aggregation node (waits for all experts)
    workflow.add_node("aggregate_expert_reports", aggregate_reports)

    # === EDGES ===

    # Start with triage
    workflow.add_edge(START, "triage")
    workflow.add_edge("triage", "delegate")

    # Parallel expert dispatch using Send API
    workflow.add_conditional_edges(
        "delegate",
        route_to_experts,
        {
            "parallel_experts": ["ran_expert", "core_expert", "transport_expert",
                                 "security_expert", "qos_expert"],
            "aggregate": "aggregate_expert_reports",
        }
    )

    # All experts lead to aggregation
    for expert in ["ran_expert", "core_expert", "transport_expert",
                   "security_expert", "qos_expert"]:
        workflow.add_edge(expert, "aggregate_expert_reports")

    # Aggregation leads to correlation
    workflow.add_edge("aggregate_expert_reports", "correlate")
    workflow.add_edge("correlate", "compile")

    # Conditional: iterate or submit
    workflow.add_conditional_edges(
        "compile",
        check_analysis_complete,
        {
            "complete": "submit",
            "iterate": "delegate",
        }
    )

    workflow.add_edge("submit", END)

    # Add checkpointing for long-running analysis
    memory = MemorySaver()

    return workflow.compile(checkpointer=memory)


# === Router Functions ===

def route_to_experts(state: OrchestratorState) -> list:
    """
    Route to appropriate experts based on triage results.
    Uses LangGraph's Send API for parallel execution.
    """
    from langgraph.types import Send

    experts_needed = state["experts_to_invoke"]
    sends = []

    for expert in experts_needed:
        if expert == "ran":
            sends.append(Send("ran_expert", state))
        elif expert == "core":
            sends.append(Send("core_expert", state))
        elif expert == "transport":
            sends.append(Send("transport_expert", state))
        elif expert == "security":
            sends.append(Send("security_expert", state))
        elif expert == "qos":
            sends.append(Send("qos_expert", state))

    if not sends:
        # No experts needed, go directly to aggregate
        return "aggregate"

    return sends


def check_analysis_complete(state: OrchestratorState) -> str:
    """Check if analysis is complete or needs more iteration"""
    if state["needs_deeper_analysis"] and state["iteration_count"] < state["max_iterations"]:
        return "iterate"
    return "complete"
```

### 6.2 Parallel Expert Execution

```python
# src/cellular_agent/parallel_execution.py
import asyncio
from typing import List, Dict

async def run_experts_parallel(
    state: OrchestratorState,
    experts: Dict[str, ExpertAgentBase]
) -> List[ExpertReport]:
    """
    Run multiple expert agents in parallel for faster troubleshooting.
    """
    experts_to_run = state["experts_to_invoke"]

    # Create tasks for parallel execution
    tasks = []
    for expert_name in experts_to_run:
        if expert_name in experts:
            expert = experts[expert_name]
            task = expert.analyze(
                task_context=state["task_description"],
                orchestrator_guidance=state["initial_assessment"],
                previous_findings=state.get("expert_reports", [])
            )
            tasks.append(task)

    # Wait for all experts to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Handle results and exceptions
    reports = []
    for result in results:
        if isinstance(result, Exception):
            reports.append({
                "expert_name": "unknown",
                "domain": "error",
                "findings": f"Expert failed: {str(result)}",
                "confidence": 0.0,
                "suspected_root_causes": [],
                "evidence": [],
                "recommended_actions": [],
            })
        else:
            reports.append(result)

    return reports
```

### 6.3 Human-in-the-Loop Support

```python
# src/cellular_agent/human_in_loop.py
from langgraph.types import interrupt

def orchestrator_compile_with_review(state: OrchestratorState) -> OrchestratorState:
    """
    Compile diagnosis with optional human review for critical issues.
    """
    # Compile preliminary diagnosis
    diagnosis = compile_diagnosis(state)

    # For critical severity or low confidence, request human review
    if state["severity"] == "critical" or diagnosis["confidence_score"] < 0.7:
        # Interrupt and wait for human approval
        human_feedback = interrupt({
            "type": "review_request",
            "preliminary_diagnosis": diagnosis,
            "expert_reports": state["expert_reports"],
            "question": "Please review the diagnosis. Approve, modify, or request additional analysis.",
        })

        if human_feedback.get("action") == "approve":
            return {**state, "final_diagnosis": diagnosis["final_diagnosis"]}
        elif human_feedback.get("action") == "modify":
            return {**state, "final_diagnosis": human_feedback["modified_diagnosis"]}
        elif human_feedback.get("action") == "more_analysis":
            return {
                **state,
                "needs_deeper_analysis": True,
                "experts_to_invoke": human_feedback.get("additional_experts", []),
            }

    return {**state, "final_diagnosis": diagnosis["final_diagnosis"]}
```

---

## 7. MCP Server Tools Reference

### 7.1 RAN MCP Server Tools

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_gnb_metrics` | `gnb_id: str` | Cell-level KPIs (PRB, users, throughput) |
| `get_ue_measurements` | `ue_id: str` | UE RF measurements (RSRP, RSRQ, SINR, CQI) |
| `get_handover_history` | `ue_id: str, duration_min: int` | Handover event timeline |
| `analyze_interference` | `cell_id: str` | Inter-cell interference analysis |
| `get_rrc_state_transitions` | `ue_id: str` | RRC state machine history |
| `check_beam_alignment` | `gnb_id: str, ue_id: str` | Beamforming status |
| `get_coverage_map` | `area_id: str` | Coverage heatmap data |
| `get_prb_utilization` | `cell_id: str, duration_min: int` | PRB usage over time |
| `get_neighbor_relations` | `cell_id: str` | Neighbor cell configuration |
| `get_antenna_config` | `gnb_id: str` | Antenna tilt, azimuth, power |

### 7.2 Core Network MCP Server Tools

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_amf_status` | - | AMF health, connected gNBs, registered UEs |
| `get_smf_status` | - | SMF health, active PDU sessions |
| `get_upf_status` | - | UPF health, tunnel count, throughput |
| `get_pdu_session_info` | `session_id: str` | Session details (QoS, UPF, IPs) |
| `get_subscriber_profile` | `supi: str` | UDM subscription data |
| `check_authentication_status` | `ue_id: str` | Auth context and history |
| `trace_registration_flow` | `ue_id: str` | Registration procedure trace |
| `get_slice_stats` | `slice_id: str` | Slice metrics and SLA status |
| `query_sbi_logs` | `nf_pair: str, duration_min: int` | NF-to-NF communication logs |
| `get_nf_service_status` | `nf_id: str` | NF service endpoints status |

### 7.3 Transport MCP Server Tools

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_link_status` | `link_id: str` | Link operational status |
| `get_link_utilization` | `link_id: str, duration_min: int` | Bandwidth usage |
| `ping_transport_path` | `src: str, dst: str` | End-to-end ICMP test |
| `traceroute_path` | `src: str, dst: str` | Path hop analysis |
| `get_latency_jitter` | `path_id: str, duration_min: int` | Delay and jitter metrics |
| `get_ptp_status` | `node_id: str` | PTP synchronization status |
| `get_synce_status` | `node_id: str` | SyncE clock status |
| `get_routing_table` | `router_id: str` | IP routing table |
| `get_mpls_lsp_status` | `lsp_id: str` | MPLS tunnel status |
| `get_interface_errors` | `interface_id: str` | Error counters (CRC, drops) |

### 7.4 Security MCP Server Tools

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_security_alarms` | `severity: str, duration_min: int` | Security alerts |
| `detect_rogue_cells` | `area_id: str` | Rogue base station detection |
| `analyze_auth_failures` | `ue_id: str, duration_min: int` | Auth failure patterns |
| `get_signaling_stats` | `interface: str, duration_min: int` | NAS/RRC message volumes |
| `check_encryption_status` | `ue_id: str` | Cipher and integrity status |
| `detect_signaling_storm` | `threshold: int` | DoS detection |
| `get_imsi_exposure_events` | `duration_min: int` | Privacy violation alerts |
| `analyze_handover_anomalies` | `cell_id: str` | Suspicious mobility patterns |
| `get_nas_message_log` | `ue_id: str, duration_min: int` | NAS protocol trace |

### 7.5 Telemetry/KPI MCP Server Tools

| Tool | Parameters | Description |
|------|------------|-------------|
| `get_kpi_trends` | `kpi_name: str, scope: str, duration_hours: int` | KPI time series |
| `get_kpi_baseline` | `kpi_name: str, scope: str` | Historical baseline values |
| `check_sla_compliance` | `slice_id: str` | SLA status and violations |
| `get_traffic_volume` | `scope: str, duration_hours: int` | Traffic statistics |
| `analyze_peak_hours` | `scope: str, days: int` | Peak traffic analysis |
| `get_e2e_latency` | `src_ue: str, dst: str, duration_min: int` | End-to-end delay |
| `get_active_alarms` | `severity: str` | Current alarm list |
| `correlate_events` | `time_window_min: int` | Cross-domain event correlation |
| `forecast_capacity` | `scope: str, horizon_days: int` | Capacity prediction |

---

## 8. Cellular Environment Classes

### 8.1 Base Environment Class

```python
# src/cellular_env/base.py
from typing import Dict, List
from collections import defaultdict

class CellularEnvBase:
    """Base class for cellular network environments."""

    def __init__(self):
        self.name = None
        self.desc = None

        # RAN components
        self.gnbs = []              # gNodeBs (5G base stations)
        self.enbs = []              # eNodeBs (4G base stations)
        self.cells = []             # Cells (sectors)
        self.ues = []               # User Equipment

        # Core Network Functions
        self.core_nfs = {
            "amf": [],              # Access and Mobility Management Function
            "smf": [],              # Session Management Function
            "upf": [],              # User Plane Function
            "udm": [],              # Unified Data Management
            "ausf": [],             # Authentication Server Function
            "nrf": [],              # Network Repository Function
            "pcf": [],              # Policy Control Function
            "nssf": [],             # Network Slice Selection Function
        }

        # Transport components
        self.transport_routers = []
        self.transport_switches = []
        self.fronthaul_links = []
        self.backhaul_links = []

        # Network slices
        self.slices = {}

    def load_components(self):
        """Categorize network components by type"""
        pass

    def deploy(self):
        """Deploy cellular network simulation"""
        raise NotImplementedError

    def undeploy(self):
        """Tear down the simulation"""
        raise NotImplementedError

    def get_topology(self) -> dict:
        """Return network topology"""
        return {
            "ran": self._get_ran_topology(),
            "core": self._get_core_topology(),
            "transport": self._get_transport_topology(),
        }

    def get_info(self) -> str:
        """Generate network summary"""
        self.load_components()
        summary = f"Network: {self.name}\n"
        summary += f"Description: {self.desc}\n"
        summary += f"gNodeBs: {len(self.gnbs)}, Cells: {len(self.cells)}, UEs: {len(self.ues)}\n"
        summary += f"Core NFs: AMF({len(self.core_nfs['amf'])}), SMF({len(self.core_nfs['smf'])}), UPF({len(self.core_nfs['upf'])})\n"
        return summary
```

### 8.2 Example Cellular Scenarios

| Scenario | Description | Components |
|----------|-------------|------------|
| `urban_macro_5g` | Dense urban 5G deployment | Multiple gNBs, high UE density, network slicing |
| `rural_coverage` | Sparse rural coverage | Few gNBs, large cells, edge coverage issues |
| `enterprise_campus` | Private 5G campus | Small cells, low latency requirements |
| `highway_mobility` | High-speed mobility scenario | Frequent handovers, Doppler effects |
| `stadium_capacity` | Massive event crowd | Capacity exhaustion, small cells |
| `iot_massive` | Massive IoT deployment | Many devices, low data rate, power saving |

---

## 9. Cellular Problem Categories

```python
# src/cellular_env/problems/problem_base.py
from enum import StrEnum

class CellularRootCauseCategory(StrEnum):
    def __new__(cls, value, description):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.description = description
        return obj

    # RAN Issues
    RF_INTERFERENCE = ("rf_interference", "Inter-cell interference, external interference, PIM")
    COVERAGE_HOLE = ("coverage_hole", "Poor signal coverage, shadow fading, indoor penetration")
    CAPACITY_EXHAUSTION = ("capacity_exhaustion", "PRB exhaustion, scheduling overload")
    HARDWARE_FAILURE = ("hardware_failure", "Antenna, RRU, BBU, or fiber failures")
    HANDOVER_FAILURE = ("handover_failure", "Inter-cell, inter-frequency, or inter-RAT handover issues")
    BEAM_MISALIGNMENT = ("beam_misalignment", "5G NR beam tracking and alignment failures")

    # Core Network Issues
    AUTHENTICATION_FAILURE = ("auth_failure", "AUSF/UDM issues, credential problems, SIM issues")
    SESSION_MANAGEMENT = ("session_mgmt", "SMF/UPF session establishment/modification failures")
    MOBILITY_MANAGEMENT = ("mobility_mgmt", "AMF tracking area issues, paging failures")
    USER_PLANE_ISSUE = ("user_plane", "UPF routing, N3/N9 tunnel issues, GTP problems")
    SLICE_FAILURE = ("slice_failure", "NSSF selection failure, slice capacity exhaustion")

    # Transport Issues
    BACKHAUL_CONGESTION = ("backhaul_congestion", "N2/N3/F1/Xn interface congestion")
    FRONTHAUL_LATENCY = ("fronthaul_latency", "eCPRI timing issues, jitter")
    TRANSPORT_FAILURE = ("transport_failure", "Router/switch failures, fiber cuts")
    SYNC_FAILURE = ("sync_failure", "PTP/SyncE/GPS timing synchronization issues")

    # Service Issues
    SLICE_SLA_VIOLATION = ("slice_sla", "Network slice SLA not met")
    QOS_DEGRADATION = ("qos_degradation", "QoS flow issues, packet loss/delay/jitter")

    # Security Issues
    ROGUE_BASE_STATION = ("rogue_bs", "IMSI catcher, false base station")
    SIGNALING_STORM = ("signaling_storm", "NAS/RRC flooding, DoS attacks")
    AUTH_ATTACK = ("auth_attack", "Replay attacks, credential compromise")
    PRIVACY_VIOLATION = ("privacy_violation", "SUPI exposure, location tracking")
```

---

## 10. Complete File Structure

```
src/
├── cellular_agent/
│   ├── __init__.py
│   ├── orchestrator_agent.py        # Main orchestrator (coordinates experts)
│   ├── workflow.py                  # LangGraph workflow definition
│   ├── parallel_execution.py        # Parallel expert execution
│   ├── human_in_loop.py             # Human review integration
│   ├── domain_agents/
│   │   ├── __init__.py
│   │   ├── expert_base.py           # Base class for expert agents
│   │   ├── ran_expert_agent.py      # RAN specialist
│   │   ├── core_expert_agent.py     # Core network specialist
│   │   ├── transport_expert_agent.py # Transport specialist
│   │   ├── security_expert_agent.py # Security analyst
│   │   ├── qos_expert_agent.py      # QoS/Performance specialist
│   │   └── submission_agent.py      # Final output formatter
│   ├── llm/
│   │   ├── __init__.py
│   │   └── model_factory.py         # LLM backend factory
│   └── utils/
│       ├── __init__.py
│       ├── mcp_servers.py           # MCP server configuration
│       └── logger.py                # Logging utilities
│
├── cellular_env/
│   ├── __init__.py
│   ├── base.py                      # CellularEnvBase class
│   ├── scenarios/
│   │   ├── __init__.py
│   │   ├── urban_macro_5g.py        # Dense urban scenario
│   │   ├── rural_coverage.py        # Rural coverage scenario
│   │   ├── enterprise_campus.py     # Private 5G campus
│   │   ├── highway_mobility.py      # High-speed mobility
│   │   ├── stadium_capacity.py      # Massive event scenario
│   │   └── iot_massive.py           # Massive IoT scenario
│   ├── problems/
│   │   ├── __init__.py
│   │   ├── problem_base.py          # Root cause categories
│   │   ├── ran_problems.py          # RAN-specific faults
│   │   ├── core_problems.py         # Core network faults
│   │   ├── transport_problems.py    # Transport faults
│   │   └── security_problems.py     # Security threats
│   └── generator/
│       ├── __init__.py
│       ├── fault_injector.py        # Fault injection base
│       ├── rf_impairment.py         # RF signal impairments
│       ├── nf_failure.py            # NF crash/restart
│       └── traffic_generator.py     # Load generation
│
├── cellular_service/
│   ├── __init__.py
│   ├── mcp_server/
│   │   ├── __init__.py
│   │   ├── ran_mcp_server.py        # RAN tools
│   │   ├── core_mcp_server.py       # Core network tools
│   │   ├── transport_mcp_server.py  # Transport tools
│   │   ├── security_mcp_server.py   # Security tools
│   │   ├── telemetry_mcp_server.py  # KPI/PM counter tools
│   │   ├── alarm_mcp_server.py      # Alarm tools
│   │   └── task_mcp_server.py       # Submission tools
│   └── api/
│       ├── __init__.py
│       ├── ueransim_api.py          # UERANSIM interface
│       ├── open5gs_api.py           # Open5GS interface
│       └── influxdb_api.py          # Telemetry database
│
├── cellular_evaluator/
│   ├── __init__.py
│   ├── llm_judge.py                 # LLM-based evaluation
│   ├── metrics.py                   # Evaluation metrics
│   └── trace_parser.py              # Agent trace parser
│
└── scripts/
    ├── step1_cellular_env_start.py  # Deploy network
    ├── step2_fault_inject.py        # Inject faults
    ├── step3_agent_run.py           # Run troubleshooting
    ├── step4_result_eval.py         # Evaluate results
    └── run_benchmark.py             # Full benchmark suite
```

---

## 11. Implementation Roadmap

### Phase 1: Foundation (Weeks 1-2)
1. Set up cellular network simulator (UERANSIM + Open5GS)
2. Create `CellularEnvBase` class with deploy/undeploy
3. Implement basic MCP servers (RAN, Core)
4. Test tool integration with simple scenarios

### Phase 2: Expert Agents (Weeks 3-4)
1. Implement `ExpertAgentBase` class
2. Create all 5 expert agents with specialized prompts
3. Test each expert independently
4. Implement MCP tool coverage for each domain

### Phase 3: Orchestrator (Weeks 5-6)
1. Implement `CellularOrchestratorAgent`
2. Build LangGraph workflow with conditional routing
3. Add parallel expert execution
4. Implement cross-domain correlation logic

### Phase 4: Problem Library (Weeks 7-8)
1. Define all root cause categories
2. Implement fault injection for each category
3. Create ground truth data structure
4. Build 100+ troubleshooting incidents

### Phase 5: Evaluation & Benchmarking (Weeks 9-10)
1. Implement LLM-based evaluation
2. Create evaluation metrics (accuracy, time, confidence)
3. Run full benchmark suite
4. Document results and tune prompts

---

## 12. Technology Stack

| Component | Recommendation | Notes |
|-----------|---------------|-------|
| **Network Simulator** | UERANSIM + Open5GS | Open source 5G SA stack |
| **Alternative** | srsRAN, ns-3-NR | For more detailed simulations |
| **Agent Framework** | LangChain + LangGraph | State management, workflow orchestration |
| **Tool Protocol** | MCP (FastMCP) | Standardized tool exposure |
| **LLM Backend** | GPT-4o, Claude, DeepSeek | Multi-model support |
| **Local LLM** | Ollama (Llama 3.1, Qwen) | Cost-effective testing |
| **Observability** | Langfuse + Langsmith | Tracing and evaluation |
| **Telemetry DB** | InfluxDB / Prometheus | Time-series metrics |
| **Container Orchestration** | Docker Compose / Kubernetes | NF deployment |

---

## 13. Summary

This expanded architecture introduces a **hierarchical multi-agent system** for cellular network troubleshooting:

### Key Improvements over NIKA's Basic Architecture:

1. **Orchestrator-Expert Pattern**: Central orchestrator performs triage, delegates to experts, and compiles final diagnosis
2. **5 Specialized Expert Agents**: Domain experts for RAN, Core, Transport, Security, and QoS
3. **Deeper Troubleshooting**: Each expert has 10+ specialized tools and domain-specific prompts
4. **Cross-Domain Correlation**: Orchestrator correlates findings across network layers
5. **Iterative Analysis**: Can request additional expert analysis when confidence is low
6. **Human-in-the-Loop**: Critical issues can be escalated for human review
7. **Parallel Execution**: Experts run concurrently for faster troubleshooting

### Workflow Summary:

```
User Report → Orchestrator Triage → Delegate to Experts (parallel)
                                           ↓
RAN Expert ──┐
Core Expert ──┼─→ Aggregate → Correlate → Compile Final Diagnosis
Transport Expert
Security Expert
QoS Expert ───┘
                                           ↓
                              (Low confidence?) ──→ Iterate
                                           ↓
                              Submit Final Report
```

This architecture mirrors how real telecom NOC teams operate, with L1/L2/L3 escalation and domain experts collaborating on complex issues.
