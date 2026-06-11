"""Local repro: verify parallel analyst fan-in works with TypedDict schema
and fails with plain dict schema (the __root__ crash seen on Railway)."""
from typing import Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, END


class TradingState(TypedDict, total=False):
    ticker: str
    asset_type: str
    market_report: str
    sentiment_report: str
    news_report: str
    fundamentals_report: str
    options_report: str
    action: str
    stop_loss: Optional[float]
    take_profit: Optional[float]


def enrich(state):
    return {**state, "ticker": state["ticker"]}

def a1(state): return {"market_report": "m"}
def a2(state): return {"sentiment_report": "s"}
def a3(state): return {"news_report": "n"}
def a4(state): return {"fundamentals_report": "f"}
def a5(state): return {"options_report": "o"}

def decide(state):
    return {**state, "action": "HOLD", "stop_loss": 1.0, "take_profit": 2.0}


def build(schema):
    g = StateGraph(schema)
    g.add_node("enrich", enrich)
    for name, fn in [("a1", a1), ("a2", a2), ("a3", a3), ("a4", a4), ("a5", a5)]:
        g.add_node(name, fn)
        g.add_edge("enrich", name)
        g.add_edge(name, "decide")
    g.add_node("decide", decide)
    g.set_entry_point("enrich")
    g.add_edge("decide", END)
    return g.compile()


# 1. Old behavior: plain dict schema must crash with __root__ error
try:
    build(dict).invoke({"ticker": "TEST", "asset_type": "prediction"})
    print("UNEXPECTED: dict schema did not crash")
except Exception as e:
    print(f"dict schema crashed as expected: {type(e).__name__}: {str(e)[:80]}")

# 2. New behavior: TypedDict schema must succeed
final = build(TradingState).invoke({"ticker": "TEST", "asset_type": "prediction"})
assert final["market_report"] == "m"
assert final["options_report"] == "o"
assert final["action"] == "HOLD"
assert final["stop_loss"] == 1.0
print("TypedDict schema: parallel fan-in OK, all keys present:", sorted(final.keys()))
