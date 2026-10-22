"""Assemble the LangGraph orchestrator (OR-1)."""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph import nodes
from app.graph.state import OpsState

AGENT_NODES = ["alert_correlation", "incident_triage", "sr_fulfilment", "kb_curation"]


def build_graph():
    g = StateGraph(OpsState)
    for name, fn in nodes.NODE_FUNCS.items():
        g.add_node(name, fn)
    g.add_edge(START, "route_event")
    g.add_conditional_edges("route_event", nodes.route_target,
                            {n: n for n in (*AGENT_NODES, "p1_surge", "finalize")})
    for a in AGENT_NODES:
        g.add_edge(a, "guardrail")
    g.add_conditional_edges("guardrail", nodes.after_guardrail,
                            {"execute": "execute", "human_queue": "human_queue", "finalize": "finalize"})
    g.add_conditional_edges("execute", nodes.after_execute, {"human_queue": "human_queue", "finalize": "finalize"})
    g.add_edge("human_queue", "finalize")
    g.add_conditional_edges("finalize", nodes.after_finalize, {"p1_surge": "p1_surge", "__end__": END})
    g.add_edge("p1_surge", END)
    return g.compile()
