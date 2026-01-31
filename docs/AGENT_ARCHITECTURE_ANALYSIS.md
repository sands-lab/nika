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

### 3.2 Proposed Cellular Agent Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                      CellularTroubleshootingAgent                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │                    LangGraph StateGraph                             │  │
│  │                                                                     │  │
│  │   START ──► RadioDiagnosisAgent ──► CoreDiagnosisAgent             │  │
│  │                                            │                        │  │
│  │                                            ▼                        │  │
│  │                                    SubmissionAgent ──► END          │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │  RAN MCP Server │   │ Core MCP Server │   │Telemetry Server │
    │  - gNB metrics  │   │ - AMF/SMF/UPF   │   │ - PM counters   │
    │  - RRC stats    │   │ - Session mgmt  │   │ - KPIs          │
    │  - RF analysis  │   │ - Subscriber    │   │ - Alarms        │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
```

### 3.3 New Environment Classes

```python
# src/cellular_env/base.py
class CellularEnvBase:
    """Base class for cellular network environments."""

    def __init__(self):
        self.gnbs = []           # gNodeBs
        self.ues = []            # User Equipment
        self.core_nfs = {}       # Core Network Functions (AMF, SMF, UPF, etc.)
        self.transport_nodes = [] # Routers, switches in backhaul

    def deploy(self):
        """Deploy cellular network simulation (e.g., UERANSIM + Open5GS)"""
        pass

    def get_coverage_map(self) -> dict:
        """Return cell coverage and interference patterns"""
        pass

    def get_subscriber_sessions(self) -> list:
        """Return active PDU sessions"""
        pass
```

### 3.4 Cellular-Specific MCP Tools

**RAN MCP Server (`ran_mcp_server.py`):**

```python
@mcp.tool()
def get_gnb_metrics(gnb_id: str) -> dict:
    """Get gNodeB performance metrics (PRB utilization, active UEs, throughput)"""
    pass

@mcp.tool()
def get_ue_measurements(ue_id: str) -> dict:
    """Get UE radio measurements (RSRP, RSRQ, SINR, CQI)"""
    pass

@mcp.tool()
def get_handover_history(ue_id: str, duration_min: int = 60) -> list:
    """Get recent handover events for a UE"""
    pass

@mcp.tool()
def analyze_interference(cell_id: str) -> dict:
    """Analyze inter-cell interference for a given cell"""
    pass

@mcp.tool()
def get_rrc_state_transitions(ue_id: str) -> list:
    """Track RRC state transitions (IDLE/CONNECTED/INACTIVE)"""
    pass

@mcp.tool()
def check_beam_alignment(gnb_id: str, ue_id: str) -> dict:
    """Check beamforming alignment between gNB and UE"""
    pass
```

**Core Network MCP Server (`core_mcp_server.py`):**

```python
@mcp.tool()
def get_amf_status() -> dict:
    """Get AMF (Access and Mobility Management Function) status"""
    pass

@mcp.tool()
def get_pdu_session_info(session_id: str) -> dict:
    """Get PDU session details (QoS, UPF path, data rates)"""
    pass

@mcp.tool()
def get_subscriber_profile(supi: str) -> dict:
    """Get subscriber profile from UDM"""
    pass

@mcp.tool()
def trace_user_plane_path(ue_id: str, destination: str) -> list:
    """Trace packet path from UE through UPF to destination"""
    pass

@mcp.tool()
def get_slice_stats(slice_id: str) -> dict:
    """Get network slice statistics and SLA compliance"""
    pass

@mcp.tool()
def check_authentication_status(ue_id: str) -> dict:
    """Check UE authentication and security context"""
    pass
```

**Telemetry MCP Server (`telecom_telemetry_mcp_server.py`):**

```python
@mcp.tool()
def query_pm_counters(ne_id: str, counter_group: str, duration_min: int) -> dict:
    """Query Performance Management counters from network element"""
    pass

@mcp.tool()
def get_active_alarms(severity: str = "all") -> list:
    """Get active alarms filtered by severity (critical/major/minor/warning)"""
    pass

@mcp.tool()
def get_kpi_trends(kpi_name: str, cell_id: str, duration_hours: int) -> list:
    """Get KPI trends (e.g., call_drop_rate, handover_success_rate)"""
    pass

@mcp.tool()
def correlate_events(time_window_min: int = 30) -> list:
    """Correlate events across RAN and Core within time window"""
    pass
```

### 3.5 Cellular Problem Categories

```python
class CellularRootCauseCategory(StrEnum):
    # RAN Issues
    RF_INTERFERENCE = ("rf_interference", "Inter-cell interference, external interference")
    COVERAGE_HOLE = ("coverage_hole", "Poor signal coverage, shadow fading")
    CAPACITY_EXHAUSTION = ("capacity_exhaustion", "PRB exhaustion, high load")
    HARDWARE_FAILURE = ("hardware_failure", "Antenna, RRU, BBU failures")
    HANDOVER_FAILURE = ("handover_failure", "Inter-cell or inter-RAT handover issues")

    # Core Network Issues
    AUTHENTICATION_FAILURE = ("auth_failure", "AUSF/UDM issues, credential problems")
    SESSION_MANAGEMENT = ("session_mgmt", "SMF/UPF session establishment failures")
    MOBILITY_MANAGEMENT = ("mobility_mgmt", "AMF tracking area issues, paging failures")
    USER_PLANE_ISSUE = ("user_plane", "UPF routing, N3/N9 tunnel issues")

    # Transport Issues
    BACKHAUL_CONGESTION = ("backhaul_congestion", "F1/E1/Xn interface congestion")
    FRONTHAUL_LATENCY = ("fronthaul_latency", "eCPRI timing issues")
    TRANSPORT_FAILURE = ("transport_failure", "Router/switch failures in transport")

    # Service Issues
    SLICE_SLA_VIOLATION = ("slice_sla", "Network slice SLA not met")
    QOS_DEGRADATION = ("qos_degradation", "QoS flow issues, packet loss/delay")

    # Security Issues
    ROGUE_BASE_STATION = ("rogue_bs", "False base station detected")
    SIGNALING_STORM = ("signaling_storm", "Excessive NAS/RRC signaling")
```

### 3.6 Cellular Diagnosis Agent System Prompt

```python
CELLULAR_DIAGNOSIS_PROMPT = """
You are a wireless telecom network troubleshooting expert specializing in 4G/5G networks.

Focus on:
1. **Detection**: Identify if there is a service anomaly (call drops, data issues, coverage problems)
2. **Localization**: Pinpoint the faulty component (UE, gNB, Core NF, transport node)
3. **Root Cause Analysis**: Determine the underlying cause (RF issue, config error, hardware failure, etc.)

Troubleshooting approach:
- Start with end-user symptoms (KPIs, alarms, subscriber complaints)
- Check radio conditions (RSRP, SINR, interference levels)
- Verify Core network connectivity and session state
- Examine transport network health
- Correlate events across domains (RAN, Core, Transport)

Use the provided tools to gather information. Do not guess - verify each hypothesis with data.
Common patterns to look for:
- High call drop rate → check handover config, interference, coverage
- Slow data → check PRB utilization, backhaul congestion, QoS settings
- Authentication failures → check AUSF/UDM, subscriber profile, SIM issues
- Intermittent connectivity → check RRC state transitions, beam alignment
"""
```

### 3.7 Example Cellular Scenarios

| Scenario | Description | Components |
|----------|-------------|------------|
| `urban_macro_5g` | Dense urban 5G deployment | Multiple gNBs, high UE density, slicing |
| `rural_coverage` | Sparse rural coverage | Few gNBs, large cells, edge coverage issues |
| `enterprise_campus` | Private 5G campus | Small cells, low latency requirements |
| `highway_mobility` | High-speed mobility scenario | Frequent handovers, Doppler effects |
| `stadium_capacity` | Massive event crowd | Capacity exhaustion, small cells |
| `iot_massive` | Massive IoT deployment | Many devices, low data rate, power saving |

---

## 4. Implementation Roadmap

### Phase 1: Environment Setup
1. Set up cellular network simulator (UERANSIM + Open5GS or srsRAN)
2. Create `CellularEnvBase` class with deploy/undeploy methods
3. Implement container orchestration for Core NFs

### Phase 2: MCP Tools Development
1. Implement RAN MCP server with gNB/UE metrics tools
2. Implement Core MCP server with NF interaction tools
3. Implement Telemetry MCP server for PM counters and alarms

### Phase 3: Agent Development
1. Create `RadioDiagnosisAgent` for RAN-focused troubleshooting
2. Create `CoreDiagnosisAgent` for Core network analysis
3. Develop specialized prompts for each agent domain

### Phase 4: Problem Library
1. Define cellular root cause categories
2. Implement fault injection mechanisms (RF impairments, NF crashes, etc.)
3. Create ground truth data for evaluation

### Phase 5: Integration & Benchmarking
1. Build end-to-end workflow with LangGraph
2. Create evaluation metrics (time-to-detect, accuracy, etc.)
3. Build benchmark suite with diverse scenarios

---

## 5. Key Files to Create

```
src/
├── cellular_agent/
│   ├── react_agent.py              # Main orchestrator
│   ├── domain_agents/
│   │   ├── radio_diagnosis_agent.py
│   │   ├── core_diagnosis_agent.py
│   │   └── submission_agent.py
│   ├── llm/
│   │   └── model_factory.py
│   └── utils/
│       └── mcp_servers.py
├── cellular_env/
│   ├── base.py                     # CellularEnvBase
│   ├── scenarios/
│   │   ├── urban_macro_5g.py
│   │   ├── rural_coverage.py
│   │   └── enterprise_campus.py
│   └── generator/
│       └── fault_injector.py
├── cellular_service/
│   └── mcp_server/
│       ├── ran_mcp_server.py
│       ├── core_mcp_server.py
│       └── telecom_telemetry_mcp_server.py
└── scripts/
    ├── step1_cellular_env_start.py
    ├── step2_fault_inject.py
    ├── step3_agent_run.py
    └── step4_result_eval.py
```

---

## 6. Technology Stack Recommendations

| Component | NIKA Uses | Cellular Recommendation |
|-----------|-----------|------------------------|
| Network Simulator | Kathara (containers) | UERANSIM + Open5GS, srsRAN, ns-3 |
| Agent Framework | LangChain + LangGraph | Same (works well) |
| Tool Protocol | MCP (FastMCP) | Same (extensible) |
| LLM Backend | OpenAI, DeepSeek, Ollama | Same (model-agnostic) |
| Observability | Langfuse, LangSmith | Same + telecom-specific dashboards |
| Telemetry Storage | InfluxDB | InfluxDB, Prometheus, or vendor OSS |

---

## 7. Summary

The NIKA architecture provides an excellent template for building AI agents for cellular network troubleshooting. The key adaptations needed are:

1. **Replace network environment** (Kathara → cellular simulator)
2. **Create domain-specific MCP tools** for RAN, Core, and Transport
3. **Design specialized agents** for different network domains (Radio vs Core)
4. **Define cellular-specific problems** and fault injection mechanisms
5. **Develop evaluation metrics** aligned with telecom KPIs

The modular design of NIKA (LangGraph + MCP + domain agents) translates well to the cellular domain, where you similarly have distinct layers (RAN, Core, Transport) that require specialized expertise.
