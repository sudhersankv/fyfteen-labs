"""fyfteenFleet: natural-language controller for deterministic paper bots."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .catalog import AGENT_KINDS, OPPORTUNITY_BUCKETS, OPPORTUNITY_DEFINITION, OPPORTUNITY_FORMULA
from .fyften_keys import llm_config, stt_config
from .fyften_prompt import FYFTEN_SYSTEM
from .names import display_name, resolve_agent_name, slugify, suggest_name
from .policy import normalize as normalize_policy

HELP = (
    "Say a fleet name, then a job or a cash change. "
    "Direction trades last 6 minutes. Both agree last 4. Late closer last 3."
)

KIND_ALIASES = {
    "settlement": "settlement",
    "closer": "settlement",
    "late": "settlement",
    "late closer": "settlement",
    "settlement closer": "settlement",
    "settlement misprice": "settlement",
    "trend": "trend-rider",
    "trend-rider": "trend-rider",
    "confirmed trend": "trend-rider",
    "short trend": "trend-rider",
    "direction": "trend-rider",
    "hybrid": "hybrid",
    "agreement": "hybrid",
    "balanced hybrid": "hybrid",
    "both": "hybrid",
    "both agree": "hybrid",
}

TEMPLATE_ALIASES = {
    "settlement": "settlement-careful",
    "settlement-careful": "settlement-careful",
    "closer": "settlement-careful",
    "trend": "trend-confirmed",
    "trend-rider": "trend-confirmed",
    "trend-confirmed": "trend-confirmed",
    "hybrid": "hybrid-balanced",
    "hybrid-balanced": "hybrid-balanced",
    "agreement": "hybrid-balanced",
}

MONEY_RE = r"\$?\s*(\d+(?:\.\d+)?)"
CENTS_RE = r"(\d+(?:\.\d+)?)\s*(?:c|¢|cent|cents|%)"


def llm_settings() -> dict[str, str]:
    return llm_config()


def _tool_args(raw) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        return {}


def _kind_from_text(text: str) -> str | None:
    match = re.search(
        r"\b(late closer|settlement closer|settlement misprice|confirmed trend|"
        r"short trend|balanced hybrid|both agree|trend-rider|settlement|closer|"
        r"direction|late|trend|hybrid|agreement|both)\b",
        text)
    if not match:
        return None
    return KIND_ALIASES[match.group(1)]


def _template_for(kind: str) -> str:
    return {"settlement": "settlement-careful", "trend-rider": "trend-confirmed",
            "hybrid": "hybrid-balanced"}[kind]


def _money(text: str) -> float | None:
    match = re.search(rf"(?:with|for|budget|cash|give|add|another|more)?\s*{MONEY_RE}", text)
    return float(match.group(1)) if match else None


def _cents(text: str) -> float | None:
    match = re.search(CENTS_RE, text)
    if match:
        value = float(match.group(1))
        return value / 100 if value > 0.20 or "cent" in text or "¢" in text or "%" in text else value
    return None


TOOLS = [
    {"type": "function", "function": {
        "name": "list_roster",
        "description": "List paper agents, cash, leftover, and deploy state.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "inspect_agent",
        "description": "Inspect one agent: leftover, required edge, cash, bank, position, waiting reason.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "create_agent",
        "description": "Create a paper bot from a named template.",
        "parameters": {"type": "object", "properties": {
            "template_id": {"type": "string",
                            "enum": ["settlement-careful", "trend-confirmed", "hybrid-balanced"]},
            "name": {"type": "string"},
            "budget": {"type": "number"},
            "deployed": {"type": "boolean"},
            "threshold": {"type": "number"},
        }, "required": ["template_id", "budget"]},
    }},
    {"type": "function", "function": {
        "name": "set_threshold",
        "description": "Set required leftover edge as a fraction (0.03 = 3 cents).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "threshold": {"type": "number"}},
            "required": ["name", "threshold"]},
    }},
    {"type": "function", "function": {
        "name": "nudge_aggression",
        "description": "Make a bot a little more or less aggressive without rewriting its bucket.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "direction": {"type": "string", "enum": ["more", "less"]}},
            "required": ["name", "direction"]},
    }},
    {"type": "function", "function": {
        "name": "add_cash",
        "description": "Add paper cash to an isolated agent account.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "dollars": {"type": "number"}},
            "required": ["name", "dollars"]},
    }},
    {"type": "function", "function": {
        "name": "set_budget",
        "description": "Set the agent's allocated paper budget.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "budget": {"type": "number"}},
            "required": ["name", "budget"]},
    }},
    {"type": "function", "function": {
        "name": "deploy_agent",
        "description": "Deploy or pause an agent. Pause stops new buys.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "deployed": {"type": "boolean"}}, "required": ["name", "deployed"]},
    }},
    {"type": "function", "function": {
        "name": "retire_agent",
        "description": "Cash out any open paper position and retire the agent.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "assign_job",
        "description": "Change an agent's job. Direction trades most. Both agree is pickier. Late closer waits until the last 3 minutes.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "template_id": {"type": "string",
                            "enum": ["trend-confirmed", "hybrid-balanced", "settlement-careful"]},
            "kind": {"type": "string", "enum": ["trend-rider", "hybrid", "settlement"]}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "explain_opportunities",
        "description": "Explain leftover edge and the three jobs in plain English.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


SYSTEM = FYFTEN_SYSTEM


class Fleet:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.messages: list[dict[str, str]] = []
        self.focus: str | None = None

    def status(self) -> dict[str, Any]:
        llm = llm_settings()
        stt = stt_config()
        return {"name": "FYFTEN", "focus": self.focus,
                "llm": bool(llm["key"]), "model": llm["model"] if llm["key"] else "local-compiler",
                "stt": stt["ready"], "stt_provider": stt["provider"]}

    def _names(self) -> list[str]:
        return list(self.engine.experiments)

    def _resolve(self, text: str | None = None, explicit: str | None = None) -> str | None:
        if explicit:
            return resolve_agent_name(explicit, self._names()) or (
                explicit if explicit in self.engine.experiments else None)
        if text:
            found = resolve_agent_name(text, self._names())
            if found:
                return found
        return self.focus if self.focus in self.engine.experiments else None

    async def chat(self, text: str, reset: bool = False) -> dict[str, Any]:
        raw = (text or "").strip()
        if reset:
            self.messages, self.focus = [], None
            if not raw:
                return {"ok": True, "reply": "FYFTEN focus cleared.",
                        "help": HELP, "focus": None, "source": "local", "actions": []}
        if not raw:
            return {"ok": False, "reply": "Tap 15 and talk.",
                    "help": HELP, "focus": self.focus, "source": "local", "actions": []}
        self.messages.append({"role": "user", "content": raw})
        command = compile_local(raw, self._names(), self.focus)
        source = "local"
        if llm_settings()["key"]:
            try:
                command = await self._compile_llm(raw)
                source = "llm"
            except Exception as exc:
                if command.get("error"):
                    command = {"error": f"LLM unavailable ({exc}). {command.get('error') or HELP}"}
                source = "local"
        if command.get("say") and not command.get("action"):
            reply = command["say"]
            self.messages.append({"role": "assistant", "content": reply})
            return {"ok": True, "reply": reply, "help": HELP, "focus": self.focus,
                    "source": source, "actions": []}
        if command.get("error"):
            reply = command["error"]
            self.messages.append({"role": "assistant", "content": reply})
            return {"ok": False, "reply": reply, "help": HELP, "focus": self.focus,
                    "source": source, "actions": []}
        try:
            result = await self._run(command)
        except ValueError as exc:
            reply = str(exc)
            self.messages.append({"role": "assistant", "content": reply})
            return {"ok": False, "reply": reply, "help": HELP, "focus": self.focus,
                    "source": source, "actions": [command]}
        if result.get("name"):
            self.focus = result["name"]
        reply = result.get("reply") or "Done."
        self.messages.append({"role": "assistant", "content": reply})
        return {"ok": True, "reply": reply, "focus": self.focus, "source": source,
                "help": HELP, "actions": [command], "result": result}

    async def _compile_llm(self, text: str) -> dict[str, Any]:
        cfg = llm_settings()
        snapshot = await self._roster_brief()
        payload = {
            "model": cfg["model"],
            "temperature": 0,
            "max_tokens": 800,
            "tools": TOOLS,
            "tool_choice": "auto",
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "system", "content": (
                    f"Focused agent: {self.focus or 'none'}. "
                    f"Roster: {json.dumps(snapshot, default=str)}")},
                *self.messages[-8:],
            ],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{cfg['base']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['key']}",
                         "Content-Type": "application/json"},
                json=payload)
            if response.status_code >= 400:
                raise ValueError(f"LLM {response.status_code}: {(response.text or '')[:280]}")
            body = response.json()
        message = (body.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if isinstance(calls, dict):
            calls = [calls]
        if not calls and message.get("function_call"):
            calls = [{"function": message["function_call"]}]
        if not calls:
            content = (message.get("content") or "").strip()
            if content:
                return {"say": content}
            return {"error": "FYFTEN needs a fleet action."}
        call = calls[0]
        fn = call.get("function") or {}
        args = _tool_args(fn.get("arguments"))
        name = fn.get("name") or call.get("name")
        if not name:
            return {"error": "FYFTEN needs a fleet action."}
        return {"action": name, **args}

    async def _roster_brief(self) -> list[dict[str, Any]]:
        live = {}
        try:
            snap = await self.engine.snapshot()
            live = {row["name"]: row for row in snap.get("strategies") or []}
        except Exception:
            live = {}
        rows = []
        for exp in self.engine.experiments.values():
            row = live.get(exp.name, {})
            rows.append({
                "name": exp.name, "display_name": display_name(exp.name),
                "kind": exp.kind, "deployed": exp.deployed, "cash": exp.cash,
                "allocated_capital": exp.allocated_capital, "bank": exp.bank,
                "threshold": exp.threshold, "leftover": row.get("net_edge"),
                "waiting_for": row.get("waiting_for") or exp.waiting_for,
                "position": row.get("position") or "FLAT",
                "action": exp.current_action,
            })
        return rows

    async def _inspect(self, name: str) -> dict[str, Any]:
        exp = self.engine.experiments.get(name)
        if not exp:
            raise ValueError(f"No active agent named {name}.")
        brief = next((row for row in await self._roster_brief() if row["name"] == name), {})
        leftover = brief.get("leftover")
        waiting = brief.get("waiting_for")
        leftover_txt = "waiting" if waiting else (
            f"{(leftover or 0)*100:.1f}¢ leftover" if leftover is not None else "no leftover yet")
        reply = (
            f"{display_name(name)} is a {AGENT_KINDS.get(exp.kind, {}).get('title', exp.kind)} "
            f"{'deployed' if exp.deployed else 'paused'} on the roster. "
            f"Cash ${exp.cash:.2f}, allocated ${exp.allocated_capital:.2f}, bank ${exp.bank:.2f}. "
            f"Required leftover {exp.threshold*100:.1f}¢. Current {leftover_txt}. "
            f"Position {brief.get('position') or 'FLAT'}."
        )
        if waiting:
            reply += f" {waiting}"
        return {"name": name, "reply": reply, **brief, "policy": exp.policy}

    async def _nudge(self, name: str, direction: str) -> dict[str, Any]:
        exp = self.engine.experiments[name]
        more = direction != "less"
        step = -0.005 if more else 0.005
        threshold = min(0.20, max(0.005, round(exp.threshold + step, 3)))
        policy = dict(exp.policy)
        policy["min_confidence"] = min(0.95, max(0.10, policy["min_confidence"] + (-0.05 if more else 0.05)))
        policy["uncertainty_penalty"] = min(2.0, max(0.0, policy["uncertainty_penalty"] + (-0.15 if more else 0.15)))
        policy["cooldown_seconds"] = min(300, max(0, policy["cooldown_seconds"] + (-10 if more else 10)))
        policy["fractional_kelly"] = min(0.50, max(0.05, policy["fractional_kelly"] + (0.05 if more else -0.05)))
        if exp.kind != "settlement":
            policy["entry_start_seconds"] = min(900, max(60, policy["entry_start_seconds"] + (30 if more else -30)))
        policy = normalize_policy(policy, self.engine.s)
        await self.engine.control_agent(name, "threshold", threshold=threshold)
        result = await self.engine.control_agent(name, "policy", policy=policy)
        result["reply"] = (
            f"{'More' if more else 'Less'} aggressive on {display_name(name)}. "
            f"Required leftover is now {threshold*100:.1f}¢ "
            f"(was {exp.threshold*100:.1f}¢). Bucket is unchanged.")
        result["name"] = name
        result["threshold"] = threshold
        return result

    async def _run(self, command: dict[str, Any]) -> dict[str, Any]:
        action = command["action"]
        name = self._resolve(command.get("name"), command.get("name"))
        if action == "list_roster":
            rows = await self._roster_brief()
            deployed = [row for row in rows if row["deployed"]]
            if not rows:
                return {"reply": "The fleet is empty. Open Fleet and pick a job, or tell me to create a Direction bot."}
            lines = []
            for row in rows:
                leftover = "waiting" if row["waiting_for"] else f"{(row.get('leftover') or 0)*100:.1f}¢"
                lines.append(
                    f"{row['display_name']} · {'deployed' if row['deployed'] else 'paused'} · "
                    f"${row['cash']:.2f} cash · leftover {leftover}")
            return {"reply": (
                f"{len(rows)} in the fleet, {len(deployed)} deployed. "
                + " ".join(lines))}
        if action == "explain_opportunities":
            buckets = "; ".join(f"{b['plain_name']} ({b['window']})" for b in OPPORTUNITY_BUCKETS)
            return {"reply": f"{OPPORTUNITY_DEFINITION} {OPPORTUNITY_FORMULA}. Named buckets: {buckets}."}
        if action == "create_agent":
            catalog = {item["id"]: item for item in self.engine.catalog()["templates"]}
            template_id = command.get("template_id") or TEMPLATE_ALIASES.get(
                command.get("kind") or "", "trend-confirmed")
            template = catalog.get(template_id)
            if not template:
                raise ValueError("Unknown bot template")
            slug = slugify(command.get("name") or "") or suggest_name(self._names())
            result = await self.engine.create_agent(
                slug, template["kind"], float(command.get("budget") or 20),
                float(command["threshold"]) if command.get("threshold") is not None else template["threshold"],
                bool(command.get("deployed", True)), template["policy"])
            result["reply"] = (
                f"Created {display_name(result['name'])} from {template['title']} "
                f"with ${result['allocated_capital']:g}. "
                f"{'Deployed' if result['deployed'] else 'Paused'}.")
            return result
        if not name:
            raise ValueError("Which agent? Name one from the roster.")
        self.focus = name
        if action == "inspect_agent":
            return await self._inspect(name)
        if action == "assign_job":
            result = await self.engine.assign_job(
                name, kind=command.get("kind"), template_id=command.get("template_id"))
            return result
        if action == "nudge_aggression":
            return await self._nudge(name, command.get("direction") or "more")
        if action == "add_cash":
            result = await self.engine.control_agent(
                name, "add_cash", delta=float(command.get("dollars") or command.get("delta") or 0))
            result["reply"] = (
                f"Added paper cash to {display_name(name)}. "
                f"Cash is now ${result['cash']:.2f}, allocated ${result['allocated_capital']:.2f}.")
            return result
        if action == "set_budget":
            result = await self.engine.control_agent(
                name, "budget", budget=float(command["budget"]))
            result["reply"] = f"{display_name(name)} budget is ${result['allocated_capital']:.2f}."
            return result
        if action == "set_threshold":
            result = await self.engine.control_agent(
                name, "threshold", threshold=float(command["threshold"]))
            result["reply"] = (
                f"{display_name(name)} now needs {result['threshold']*100:.1f}¢ leftover.")
            return result
        if action == "deploy_agent":
            verb = "deploy" if command.get("deployed", True) else "pause"
            result = await self.engine.control_agent(name, verb)
            result["reply"] = f"{display_name(name)} is {result['status']}."
            return result
        if action == "retire_agent":
            result = await self.engine.control_agent(name, "retire", flatten=True)
            self.focus = None
            result["reply"] = (
                f"Cashed out and retired {display_name(name)}. "
                f"Historical trades stay on Analytics.")
            return result
        raise ValueError("FYFTEN does not know that control.")


def compile_local(text: str, names: list[str], focus: str | None = None) -> dict[str, Any]:
    lowered = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lowered:
        return {"error": "Say a command.", "help": HELP}

    if re.search(r"\b(what is an opportunity|opportunit|leftover =|named bucket)\b", lowered):
        return {"action": "explain_opportunities"}

    listing = re.search(
        r"\b(how many|list (the )?(agents|bots|roster)|who is (running|deployed)|deployed now|how many are)\b",
        lowered)
    going = re.search(r"\b(go to|open|look at|inspect|check|use)\b", lowered)
    if listing and not going:
        return {"action": "list_roster"}

    target = resolve_agent_name(lowered, names) or (
        focus if focus in names else None)
    if not target and len(names) == 1 and re.search(r"\b(him|her|this|that)\b", lowered):
        target = names[0]
    job = _kind_from_text(lowered)
    if job and target and re.search(
            r"\b(job|put|switch|assign|change|make him|make her|make this|"
            r"on direction|to direction|to late|to both)\b", lowered):
        return {"action": "assign_job", "name": target, "kind": job,
                "template_id": _template_for(job)}

    create = re.search(
        r"\b(create|spin up|new)\b.*\b(bot|agent|closer|trend|hybrid|settlement|agreement|direction)\b",
        lowered)
    if create and not re.search(r"\b(add|give)\b.*\$", lowered):
        kind = job or "trend-rider"
        named = re.search(r"(?:named|called|name)\s+([a-z0-9][a-z0-9-]{1,31})", lowered)
        return {"action": "create_agent", "template_id": _template_for(kind),
                "kind": kind, "budget": _money(lowered) or 20.0,
                "deployed": not bool(re.search(r"\bpaused?\b", lowered)),
                "name": named.group(1) if named else None,
                "threshold": _cents(lowered)}

    go = re.search(r"\b(go to|open|look at|inspect|check|use)\b", lowered)
    if re.search(r"\b(retire|cash out|cash-out|cashout|shut down|remove)\b", lowered):
        if not target:
            return {"error": "Which agent should be cashed out and retired?"}
        return {"action": "retire_agent", "name": target}

    if go and target and re.search(
            r"\b(edge|leftover|cash|balance|budget|how much|position|status|doing)\b",
            lowered) is None and not re.search(
            r"\b(aggressive|passive|retire|pause|deploy|give|add|change)\b", lowered):
        return {"action": "inspect_agent", "name": target}

    if re.search(r"\b(edge|leftover|cash|balance|budget|how much|position|status)\b", lowered) and not re.search(
            r"\b(cash out|cash-out|cashout)\b", lowered):
        if re.search(r"\b(set|change|move|drop|raise|lower)\b", lowered) and (
                "edge" in lowered or "leftover" in lowered):
            if not target:
                return {"error": "Which agent should change leftover?"}
            amount = _cents(lowered)
            if amount is None:
                return {"action": "nudge_aggression", "name": target,
                        "direction": "less" if re.search(r"\b(up|higher|pickier|passive)\b", lowered) else "more"}
            return {"action": "set_threshold", "name": target, "threshold": amount}
        if not target:
            return {"error": "Which agent? Name one from the roster."}
        return {"action": "inspect_agent", "name": target}

    if re.search(r"\b(more aggressive|less passive|too passive|a little more|change (the )?(edge|date) a little)\b", lowered):
        if not target:
            return {"error": "Which agent should be more aggressive?"}
        return {"action": "nudge_aggression", "name": target, "direction": "more"}
    if re.search(r"\b(less aggressive|more passive|too aggressive|pickier)\b", lowered):
        if not target:
            return {"error": "Which agent should be less aggressive?"}
        return {"action": "nudge_aggression", "name": target, "direction": "less"}

    if re.search(rf"\b(add|give|fund)\b.*{MONEY_RE}", lowered) or re.search(
            rf"{MONEY_RE}\s+(more|extra)", lowered):
        if not target:
            return {"error": "Which agent should get the paper cash?"}
        return {"action": "add_cash", "name": target, "dollars": _money(lowered) or 20.0}

    if re.search(r"\b(pause|stop)\b", lowered) and target:
        return {"action": "deploy_agent", "name": target, "deployed": False}
    if re.search(r"\b(deploy|run|start|resume)\b", lowered) and target:
        return {"action": "deploy_agent", "name": target, "deployed": True}

    if target and go:
        return {"action": "inspect_agent", "name": target}

    return {"error": HELP}
