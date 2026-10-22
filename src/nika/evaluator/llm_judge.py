import json

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# from agent.llm.langchain_deepseek import DeepSeekLLM
from agent.llm.model_factory import load_model

load_dotenv()

# The judge sees the ground truth, so its prompt lives with the evaluator on
# the host, never in the agent package that sandboxes receive.
LLM_JUDGE_PROMPT_TEMPLATE = """
You are an expert networking engineer acting as a judge.
You will assess the performance of an autonomous agent given:
- Ground Truth: {ground_truth}
- Action History: {trace}

Ground truth lists each diagnosis as a resource id plus a fault_type (failure ID).
Compare the agent's final submit() against those pairs, not device-name lists alone.

Evaluation criteria (each scored 1-5):
1. Relevance of the actions to the problem
2. Correctness of tools/commands used
3. Efficiency and sequence of actions
4. Clarity of justification / explanatory reasoning in the agent's actions
5. Final outcome: whether the final submission exists and matches the problem ground truth

Instructions:
- For the provided agent's actions, briefly comment on its relevance, correctness, and efficiency.
- Then give an overall evaluation: what worked well, what could be improved.
- Score each of the 5 criteria individually (1 = poor, 5 = excellent).
- Provide a final overall score from 1 to 5 with reasoning.
"""


class Score(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Score from 1 to 5.")
    comment: str = Field(
        ..., description="Comment explaining the rationale for the score."
    )


class Scores(BaseModel):
    relevance: Score = Field(
        ..., description="How relevant the agent's actions were to the problem."
    )
    correctness: Score = Field(
        ..., description="How correct the tools/commands and actions were."
    )
    efficiency: Score = Field(
        ..., description="How efficient and well-ordered the agent’s actions were."
    )
    clarity: Score = Field(
        ..., description="How clear and well-explained the agent’s reasoning was."
    )
    final_outcome: Score = Field(
        ...,
        description="Whether the final outcome existed and matched the ground truth.",
    )
    overall_score: Score = Field(
        ..., description="Overall final score summarizing the total performance."
    )


class JudgeResponse(BaseModel):
    scores: Scores = Field(
        ..., description="Per-criterion scores and evaluator comments."
    )
    overall_evaluation: str = Field(
        ..., description="High-level summary of strengths and weaknesses."
    )
    reasoning_for_overall_score: str = Field(
        ..., description="Explanation of why this overall score was given."
    )


class LLMJudge:
    def __init__(
        self, judge_llm_provider: str = "openai", judge_model: str = "gpt-5-mini"
    ):
        self.llm = load_model(llm_provider=judge_llm_provider, model=judge_model)
        self.llm = self.llm.with_structured_output(JudgeResponse)
        self.prompt = LLM_JUDGE_PROMPT_TEMPLATE

    def _parse_trace(self, trace: str) -> str:
        """Parse the agent's action history trace.
        1. Remove generation info and usage metadata.

        Args:
            trace: The raw trace string.

        Returns:
            str: The parsed trace.
        """
        new_trace = []
        for line in trace.splitlines():
            line = json.loads(line)
            if "event" in line:
                if line["event"] == "llm_start":
                    payload = line.get("prompts", "")
                    new_trace.append(
                        {
                            "timestamp": line.get("timestamp", ""),
                            "event": "LLM Prompt",
                            "payload": payload,
                        }
                    )
                elif line["event"] == "llm_end":
                    payload = line.get("text", "")
                    new_trace.append(
                        {
                            "timestamp": line.get("timestamp", ""),
                            "event": "LLM Response",
                            "payload": payload,
                        }
                    )
                else:
                    new_trace.append(line)
        return json.dumps(new_trace, ensure_ascii=False)

    def evaluate_agent(self, ground_truth: str, trace_path: str, save_path: str) -> str:
        """Evaluate the agent's performance based on the problem description, network environment info, and action history.

        Args:
            problem_description: Description of the problem.
            net_env_info: Information about the network environment.
            trace_path: Path to the file containing the agent's action history.
            save_path: Path to save the evaluation result.

        Returns:
            str: The evaluation result from the judge model.
            int: The score extracted from the evaluation result.
        """
        with open(trace_path, "r") as f:
            trace = f.read()
        trace = self._parse_trace(trace)

        self.prompt = self.prompt.format(
            ground_truth=ground_truth,
            trace=trace,
        )
        evaluation: JudgeResponse = self.llm.invoke(self.prompt)

        # Save evaluation result to file
        with open(save_path, "w+") as f:
            f.write(evaluation.model_dump_json(indent=2))

        return evaluation
