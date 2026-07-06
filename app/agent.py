import json
import re
import logging
import sys
import os
from typing import Any

from pydantic import BaseModel

from google.adk.agents import LlmAgent, Context
from google.adk.tools import AgentTool
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, Edge, START, FunctionNode
from google.adk.apps import App
from mcp.client.stdio import StdioServerParameters

from .config import config

logger = logging.getLogger("travel_security")
logger.setLevel(logging.INFO)


# ── State schema ──────────────────────────────────────────────────────
class TravelState(BaseModel):
    user_request: str = ""
    itinerary_plan: str = ""
    human_feedback: str = ""
    final_output: str = ""
    security_alert: str = ""


# ── MCP Toolset (stdio subprocess) ───────────────────────────────────
mcp_path = os.path.join(os.path.dirname(__file__), "mcp_server.py")
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_path],
        )
    )
)


# ── Sub-agents ────────────────────────────────────────────────────────
research_agent = LlmAgent(
    name="research_agent",
    model=config.model,
    instruction=(
        "Research destinations. Provide highlights, weather insights, "
        "and budget estimates."
    ),
    tools=[mcp_toolset],
)

itinerary_agent = LlmAgent(
    name="itinerary_agent",
    model=config.model,
    instruction=(
        "Create detailed day-by-day itineraries and packing lists "
        "based on destination research."
    ),
    tools=[mcp_toolset],
)


# ── Orchestrator ──────────────────────────────────────────────────────
research_tool = AgentTool(agent=research_agent)
itinerary_tool = AgentTool(agent=itinerary_agent)

orchestrator = LlmAgent(
    name="orchestrator",
    model=config.model,
    instruction=(
        "You are the lead travel concierge. "
        "Coordinate with the research and itinerary agents to fulfill "
        "the user's travel request from ctx.state['user_request']. "
        "Always call the research tool first, "
        "then use the itinerary tool to build a day-by-day plan. "
        "Store the final plan in ctx.state['itinerary_plan']."
    ),
    tools=[research_tool, itinerary_tool],
    output_key="itinerary_plan",
)


# ── Workflow function-nodes ───────────────────────────────────────────
# With parameter_binding='state' (default), ADK passes matching state
# fields as kwargs AND the optional `ctx: Context` parameter.


def set_request(ctx: Context, node_input: Any = None) -> None:
    """Captures the user's incoming message and stores it in state."""
    from google.genai import types as genai_types

    if node_input is None:
        return

    # node_input from START is types.Content; convert to plain string
    if isinstance(node_input, genai_types.Content):
        text_parts = [
            p.text for p in (node_input.parts or []) if getattr(p, "text", None)
        ]
        ctx.state["user_request"] = " ".join(text_parts)
    elif isinstance(node_input, str):
        ctx.state["user_request"] = node_input
    else:
        ctx.state["user_request"] = str(node_input)


def security_checkpoint(ctx: Context) -> None:
    """Scrubs PII, detects prompt injection, logs audit trail."""
    user_request = ctx.state.get("user_request", "")
    alert = None

    # 1. PII Scrubbing (passport numbers, credit cards)
    scrubbed = re.sub(r"\b[A-Z0-9]{8,9}\b", "[PASSPORT REDACTED]", user_request)
    scrubbed = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[CREDIT CARD REDACTED]", scrubbed)

    # 2. Prompt injection detection
    injection_keywords = [
        "ignore previous instructions",
        "system prompt",
        "bypass",
        "you are now",
    ]
    if any(kw in scrubbed.lower() for kw in injection_keywords):
        alert = "Prompt injection detected."

    # 3. Domain-specific rule
    if "10000" in scrubbed and "luxury" not in scrubbed.lower():
        alert = "High budget request without 'luxury' tag. Suspicious activity."

    # 4. Structured JSON audit log
    audit_log = {
        "event": "security_check",
        "severity": "CRITICAL" if alert else "INFO",
        "alert": alert or "none",
        "query_length": len(scrubbed),
    }
    logger.info(json.dumps(audit_log))

    # CRITICAL: set ctx.route (not return value) so ADK edge routing fires
    if alert:
        ctx.state["security_alert"] = alert
        ctx.route = "blocked"
    else:
        ctx.route = "safe"


def human_review(ctx: Context):
    """Pauses the workflow to ask the human for approval or feedback."""
    plan = ctx.state.get("itinerary_plan", "")
    return RequestInput(
        message=(
            "Review the proposed trip plan below. "
            "Say 'approve' to finalize, or provide feedback.\n\n"
            f"{plan}"
        ),
    )


def evaluate_feedback(ctx: Context) -> None:
    """Routes based on human response: approved → done, else → revise."""
    feedback_text = ""

    # The human's response arrives via resume_inputs
    if hasattr(ctx, "resume_inputs") and ctx.resume_inputs:
        for key, value in ctx.resume_inputs.items():
            if isinstance(value, str):
                feedback_text = value
                break
            if isinstance(value, dict) and "text" in value:
                feedback_text = value["text"]
                break

    if not feedback_text:
        feedback_text = ctx.state.get("human_feedback", "")

    if feedback_text.strip().lower() == "approve":
        # CRITICAL: set ctx.route so ADK edge routing fires
        ctx.route = "approved"
        return

    # Store feedback for next iteration
    ctx.state["human_feedback"] = feedback_text
    ctx.state["user_request"] = (
        ctx.state.get("user_request", "") + f"\n\nFeedback: {feedback_text}"
    )
    ctx.route = "needs_revision"


def final_output(ctx: Context) -> dict[str, Any]:
    """Returns the final result to the user."""
    alert = ctx.state.get("security_alert", "")
    if alert:
        return {"status": "error", "message": f"Security block: {alert}"}
    plan = ctx.state.get("itinerary_plan", "")
    ctx.state["final_output"] = plan
    return {"status": "success", "plan": plan}


# ── Build FunctionNode instances ──────────────────────────────────────
set_request_node = FunctionNode(func=set_request, name="set_request")
security_node = FunctionNode(func=security_checkpoint, name="security_checkpoint")

# human_review must pause execution (wait_for_output=True) so the user
# can type their approval/feedback in the ADK web UI.
# Note: wait_for_output is a BaseNode field set after FunctionNode construction.
review_node = FunctionNode(func=human_review, name="human_review")
review_node.wait_for_output = True

evaluate_node = FunctionNode(func=evaluate_feedback, name="evaluate_feedback")
output_node = FunctionNode(func=final_output, name="final_output")


# ── Assemble the Workflow (declarative graph) ─────────────────────────
workflow = Workflow(
    name="app",
    state_schema=TravelState,
    edges=[
        # START → capture user message into state → security checkpoint
        Edge(from_node=START, to_node=set_request_node),
        Edge(from_node=set_request_node, to_node=security_node),
        # security → orchestrator (safe) or → final output (blocked)
        Edge(from_node=security_node, to_node=orchestrator, route="safe"),
        Edge(from_node=security_node, to_node=output_node, route="blocked"),
        # orchestrator → human review (unconditional)
        Edge(from_node=orchestrator, to_node=review_node),
        # human review → evaluate feedback (unconditional)
        Edge(from_node=review_node, to_node=evaluate_node),
        # evaluate → final output (approved) or → orchestrator (revise)
        Edge(from_node=evaluate_node, to_node=output_node, route="approved"),
        Edge(from_node=evaluate_node, to_node=orchestrator, route="needs_revision"),
    ],
)


# ── Expose as App for ADK web / AgentLoader ───────────────────────────
# ADK's AgentLoader looks for a module-level `app` variable that is an
# instance of google.adk.apps.App (or a `root_agent` BaseAgent/BaseNode).
app = App(
    name="app",
    root_agent=workflow,
)

# Also expose root_agent alias for compatibility
root_agent = workflow
