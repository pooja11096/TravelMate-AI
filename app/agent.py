import json
import re
import logging
import sys
import os
from typing import Any

from pydantic import BaseModel

from google.adk.agents import LlmAgent, Context
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.adk.workflow import Workflow, Edge, START, FunctionNode
from google.adk.apps import App
from mcp.client.stdio import StdioServerParameters

from .config import config

logger = logging.getLogger("travel_security")
logger.setLevel(logging.INFO)


class TravelState(BaseModel):
    user_request: str = ""
    raw_data: str = ""
    itinerary_plan: str = ""
    security_alert: str = ""


mcp_path = os.path.join(os.path.dirname(__file__), "mcp_server.py")
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_path],
        )
    )
)


planner_agent = LlmAgent(
    name="planner",
    model=config.model,
    instruction=(
        "You are a travel planner. Gather all necessary information by calling the available "
        "tools in parallel where possible. Do NOT generate the final itinerary yet.\n\n"
        "Call these tools based on the user's request:\n"
        "- get_local_attractions(location): find attractions at the destination\n"
        "- get_weather_forecast(location, days): check weather for the trip dates\n"
        "- get_currency_exchange(base, target): if currency conversion is needed\n"
        "- get_flight_status(flight_number): if a flight number is given\n\n"
        "Once you have all the data, summarize it as raw facts and store in ctx.state['raw_data']."
    ),
    tools=[mcp_toolset],
    output_key="raw_data",
)


itinerary_agent = LlmAgent(
    name="itinerary_generator",
    model=config.model,
    instruction=(
        "You are a travel document designer. Using the raw data from ctx.state['raw_data'], "
        "create a polished, visually appealing markdown travel itinerary.\n\n"
        "Structure it with these sections:\n"
        "1. Destination Overview\n"
        "2. Day-by-Day Itinerary\n"
        "3. Estimated Budget\n"
        "4. Packing Checklist\n"
        "5. Safety & Etiquette Tips\n\n"
        "Use emoji icons (e.g., ✈️ 🏨 🍽️ 🎒 ☀️) and markdown formatting "
        "(headings, tables, bold) to make it presentation-ready.\n\n"
        "Store the final document in ctx.state['itinerary_plan']."
    ),
    tools=[],
    output_key="itinerary_plan",
)


def security_checkpoint(ctx: Context) -> None:
    user_request = ctx.state.get("user_request", "")
    alert = None

    scrubbed = re.sub(r"\b[A-Z0-9]{8,9}\b", "[PASSPORT REDACTED]", user_request)
    scrubbed = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[CREDIT CARD REDACTED]", scrubbed)

    injection_keywords = [
        "ignore previous instructions",
        "system prompt",
        "bypass",
        "you are now",
    ]
    if any(kw in scrubbed.lower() for kw in injection_keywords):
        alert = "Prompt injection detected."

    if "10000" in scrubbed and "luxury" not in scrubbed.lower():
        alert = "High budget request without 'luxury' tag. Suspicious activity."

    audit_log = {
        "event": "security_check",
        "severity": "CRITICAL" if alert else "INFO",
        "alert": alert or "none",
        "query_length": len(scrubbed),
    }
    logger.info(json.dumps(audit_log))

    if alert:
        ctx.state["security_alert"] = alert
        ctx.route = "blocked"
    else:
        ctx.route = "safe"


def final_output(ctx: Context) -> dict[str, Any]:
    alert = ctx.state.get("security_alert", "")
    if alert:
        return {"status": "error", "message": f"Security block: {alert}"}
    plan = ctx.state.get("itinerary_plan", "")
    return {"status": "success", "plan": plan}


security_node = FunctionNode(func=security_checkpoint, name="security_checkpoint")
output_node = FunctionNode(func=final_output, name="final_output")

workflow = Workflow(
    name="app",
    state_schema=TravelState,
    edges=[
        Edge(from_node=START, to_node=security_node),
        Edge(from_node=security_node, to_node=planner_agent, route="safe"),
        Edge(from_node=security_node, to_node=output_node, route="blocked"),
        Edge(from_node=planner_agent, to_node=itinerary_agent),
        Edge(from_node=itinerary_agent, to_node=output_node),
    ],
)

app = App(
    name="app",
    root_agent=workflow,
)

root_agent = workflow
