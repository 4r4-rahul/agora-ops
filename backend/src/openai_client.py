import os, json
from typing import List, Dict, Any
from openai import OpenAI

_client = None

def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")
        _client = OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_API_BASE","https://api.openai.com/v1"))
    return _client

def explain_option_plans(plans: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    client = get_client()
    model = os.getenv("OPENAI_MODEL","gpt-5.1")
    prompt = {
        "role":"user",
        "content":(
            "You are an options trading assistant. For each plan, return JSON with "
            "symbol, summary, risk, time_comment."
        )
    }
    messages = [
        {"role":"system","content":"You are a precise, JSON-only financial explainer."},
        prompt,
        {"role":"user","content":json.dumps(plans)}
    ]
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type":"json_object"},
        temperature=0.4,
    )
    data = json.loads(resp.choices[0].message.content)
    out = []
    for ex in data.get("explanations",[]):
        out.append({
            "symbol": ex.get("symbol",""),
            "summary": ex.get("summary",""),
            "risk": ex.get("risk",""),
            "time_comment": ex.get("time_comment",""),
        })
    return out
