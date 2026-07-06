from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools.structured import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.llm.model_factory import load_model
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.mcp_servers import harden_mcp_tools
from agent.utils.phases import SUBMISSION
from nika.utils.session import Session

load_dotenv()


class SubmissionPhase:
    """LangChain ReAct worker for the submission phase."""

    def __init__(
        self,
        session_id: str,
        llm_provider: str = "openai",
        model: str = "gpt-5-mini",
        scenario_name: str = "",
    ):
        session = Session()
        session.load_running_session(session_id=session_id)
        mcp_server_config = load_session_mcp_config(
            session_id,
            scenario_name or session.scenario_name,
        )
        self.client = MultiServerMCPClient(connections=mcp_server_config)
        self.tools = None

        self.llm = load_model(llm_provider=llm_provider, model=model)

    async def load_tools(self):
        self.tools: list[StructuredTool] = await self.client.get_tools()
        harden_mcp_tools(self.tools)
        for tool in self.tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True

    def get_agent(self):
        agent = create_agent(
            model=self.llm,
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
            tools=self.tools,
            name=SUBMISSION,
        )
        return agent
