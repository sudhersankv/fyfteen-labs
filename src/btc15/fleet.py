"""FYFTEN: text chatbot that calls validated paper-bot tools."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .catalog import AGENT_KINDS, EDGE_FORMULA, LEGACY_KINDS, REASON_LABELS
from .fyften_keys import llm_config
from .fyften_prompt import FYFTEN_SYSTEM
from .names import display_name, resolve_agent_name, slugify
from .policy import ADVANCED_FIELDS, normalize as normalize_policy

HELP = (
    "Name a bot, then a template or a cash change. "
    "Templates: Fair Value, Momentum, Late Settlement."
)

KIND_ALIASES = {
    "fair value": "fair-value",
    "fair-value": "fair-value",
    "fair": "fair-value",
    "momentum": "momentum",
    "trend": "momentum",
    "direction": "momentum",
    "late settlement": "late-settlement",
    "late-settlement": "late-settlement",
    "settlement": "late-settlement",
    "late": "late-settlement",
    "closer": "late-settlement",
    "hybrid": "fair-value",
    "both agree": "fair-value",
}

MONEY_RE = r"\$?\s*(\d+(?:\.\d+)?)"
CENTS_RE = r"(\d+(?:\.\d+)?)\s*(?:c|¢|cent|cents|%)"

READ_TOOLS = {
    "list_bots", "list_roster", "inspect_bot", "inspect_agent",
    "list_templates", "explain_template", "explain_opportunities",
    "summarize_market", "summarize_bot", "why_not_traded", "summarize_best",
}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_bots",
        "description": "List paper bots, cash, template, position, and deploy state.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "inspect_bot",
        "description": "Inspect one bot: template, cash, position, last reason, leftover edge.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "list_templates",
        "description": "Show Fair Value, Momentum, and Late Settlement templates.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "explain_template",
        "description": "Explain one template in plain English.",
        "parameters": {"type": "object", "properties": {
            "template_id": {"type": "string",
                            "enum": ["fair-value", "momentum", "late-settlement"]}},
            "required": ["template_id"]},
    }},
    {"type": "function", "function": {
        "name": "create_bot",
        "description": "Create a paper bot from a template.",
        "parameters": {"type": "object", "properties": {
            "template_id": {"type": "string",
                            "enum": ["fair-value", "momentum", "late-settlement"]},
            "name": {"type": "string"},
            "budget": {"type": "number"},
            "deployed": {"type": "boolean"},
            "threshold": {"type": "number"},
        }, "required": ["template_id", "name", "budget"]},
    }},
    {"type": "function", "function": {
        "name": "assign_template",
        "description": "Switch a bot to Fair Value, Momentum, or Late Settlement.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "template_id": {"type": "string",
                            "enum": ["fair-value", "momentum", "late-settlement"]}},
            "required": ["name", "template_id"]},
    }},
    {"type": "function", "function": {
        "name": "add_cash",
        "description": "Add simulated cash to a bot.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "dollars": {"type": "number"}},
            "required": ["name", "dollars"]},
    }},
    {"type": "function", "function": {
        "name": "set_budget",
        "description": "Set a bot's allocated paper budget.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "budget": {"type": "number"}},
            "required": ["name", "budget"]},
    }},
    {"type": "function", "function": {
        "name": "set_threshold",
        "description": "Set minimum leftover edge as a fraction (0.04 = 4%).",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "threshold": {"type": "number"}},
            "required": ["name", "threshold"]},
    }},
    {"type": "function", "function": {
        "name": "set_beginner_settings",
        "description": "Change beginner settings: min edge, max order, max exposure.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "threshold": {"type": "number"},
            "max_order_dollars": {"type": "number"},
            "max_market_exposure": {"type": "number"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "update_advanced_settings",
        "description": "Update known advanced policy fields only. Unknown keys are rejected.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "entry_start_seconds": {"type": "number"},
            "entry_stop_seconds": {"type": "number"},
            "max_spread": {"type": "number"},
            "min_liquidity": {"type": "number"},
            "cooldown_seconds": {"type": "number"},
            "max_entries_per_market": {"type": "number"},
            "max_total_exposure": {"type": "number"},
            "capital_fraction": {"type": "number"},
            "exit_edge": {"type": "number"}},
            "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "deploy_bot",
        "description": "Deploy a paused bot so it can buy again.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "pause_bot",
        "description": "Pause a bot. Open positions still settle.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "pause_all",
        "description": "Pause every paper bot.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "retire_bot",
        "description": "Cash out and retire a bot. History stays.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "why_not_traded",
        "description": "Count recent HOLD/WAIT reasons for one bot.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "summarize_bot",
        "description": "Summarize a bot's cash, P&L, trades, and last reason.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "summarize_best",
        "description": "Summarize the paper bot with the highest closed P&L.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "summarize_market",
        "description": "Summarize the current BTC15 market: target, time, YES/NO, BRTI.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


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
        r"\b(late settlement|fair value|fair-value|late-settlement|"
        r"momentum|direction|settlement|closer|trend|fair|late|hybrid|both agree)\b",
        text)
    if not match:
        return None
    return KIND_ALIASES.get(match.group(1))


def _money(text: str) -> float | None:
    match = re.search(rf"(?:with|for|budget|cash|give|add|another|more)?\s*{MONEY_RE}", text)
    return float(match.group(1)) if match else None


def _cents(text: str) -> float | None:
    match = re.search(CENTS_RE, text)
    if match:
        value = float(match.group(1))
        return value / 100 if value > 0.20 or "cent" in text or "¢" in text or "%" in text else value
    return None


def _alias(action: str) -> str:
    return {
        "list_roster": "list_bots",
        "inspect_agent": "inspect_bot",
        "create_agent": "create_bot",
        "assign_job": "assign_template",
        "deploy_agent": "deploy_bot",
        "retire_agent": "retire_bot",
        "explain_opportunities": "list_templates",
        "nudge_aggression": "set_threshold",
        "change_template": "assign_template",
        "update_settings": "update_advanced_settings",
        "summarize_best": "summarize_best",
    }.get(action, action)


def _md(title: str, items: list[str]) -> str:
    lines = [f"**{title}**"] if title else []
    lines.extend(f"- {item}" for item in items if item)
    return "\n".join(lines)


class Fleet:
    def __init__(self, engine):
        self.engine = engine
        self.messages: list[dict[str, str]] = []
        self.focus: str | None = None

    def status(self) -> dict[str, Any]:
        llm = llm_settings()
        return {"name": "FYFTEN", "focus": self.focus, "llm": bool(llm["key"]),
                "model": llm["model"] if llm["key"] else "local-compiler"}

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
            return {"ok": False, "reply": "Type a message to 15.",
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
        missing = _missing_args(command, self._names(), self.focus)
        if missing:
            self.messages.append({"role": "assistant", "content": missing})
            return {"ok": False, "reply": missing, "help": HELP, "focus": self.focus,
                    "source": source, "actions": [command]}
        return await self._execute(command, source)

    async def _execute(self, command: dict[str, Any], source: str) -> dict[str, Any]:
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
                {"role": "system", "content": FYFTEN_SYSTEM},
                {"role": "system", "content": (
                    f"Focused bot: {self.focus or 'none'}. "
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
            settled = await self.engine.store.one(
                """SELECT COUNT(*) trades, COALESCE(SUM(pnl),0) net_pnl
                   FROM closed_trades WHERE experiment=?""", (exp.name,))
            rows.append({
                "name": exp.name, "display_name": display_name(exp.name),
                "kind": exp.kind, "template": AGENT_KINDS.get(exp.kind, {}).get("title", exp.kind),
                "deployed": exp.deployed, "cash": exp.cash,
                "allocated_capital": exp.allocated_capital, "bank": exp.bank,
                "threshold": exp.threshold, "leftover": row.get("net_edge"),
                "waiting_for": row.get("waiting_for") or exp.waiting_for,
                "position": row.get("position") or "FLAT",
                "reason": exp.reason, "action": exp.current_action,
                "trades": (settled or {}).get("trades") or 0,
                "net_pnl": (settled or {}).get("net_pnl") or 0,
            })
        return rows

    async def _inspect(self, name: str) -> dict[str, Any]:
        exp = self.engine.experiments.get(name)
        if not exp:
            raise ValueError(f"No active bot named {name}.")
        brief = next((row for row in await self._roster_brief() if row["name"] == name), {})
        title = AGENT_KINDS.get(exp.kind, {}).get("title", exp.kind)
        owns = brief.get("position") or "FLAT"
        reply = _md(display_name(name), [
            f"**{title}** · {'deployed' if exp.deployed else 'paused'}",
            f"Cash **${exp.cash:.2f}** · allocated **${exp.allocated_capital:.2f}**",
            f"Owns **{owns}**",
            f"Last: {exp.reason}",
        ])
        return {"name": name, "reply": reply, **brief, "policy": exp.policy}

    async def _run(self, command: dict[str, Any]) -> dict[str, Any]:
        action = _alias(command.get("action") or "")
        name = self._resolve(command.get("name"), command.get("name"))
        if action == "list_bots":
            rows = await self._roster_brief()
            deployed = [row for row in rows if row["deployed"]]
            if not rows:
                return {"reply": _md("Fleet", ["No bots yet. Create a Fair Value bot to start."])}
            noun = "bot" if len(rows) == 1 else "bots"
            items = [
                f"**{row['display_name']}** · {row['template']} · "
                f"{'deployed' if row['deployed'] else 'paused'} · "
                f"cash **${row['cash']:.2f}** · P&L **{row['net_pnl']:+.2f}** · owns {row['position']}"
                for row in rows]
            return {"reply": _md(f"Fleet — {len(rows)} {noun}, {len(deployed)} deployed", items)}
        if action == "summarize_best":
            rows = await self._roster_brief()
            if not rows:
                return {"reply": _md("Fleet", ["No bots yet."])}
            best = max(rows, key=lambda row: (row.get("net_pnl") or 0, row.get("cash") or 0))
            return await self._run({"action": "summarize_bot", "name": best["name"]})
        if action == "list_templates":
            return {"reply": _md("Templates", [
                f"**{meta['title']}** — {meta['summary']}" for meta in AGENT_KINDS.values()
            ] + [f"Edge: `{EDGE_FORMULA}`"])}
        if action == "explain_template":
            kind = LEGACY_KINDS.get(command.get("template_id") or command.get("kind") or "", "")
            meta = AGENT_KINDS.get(kind)
            if not meta:
                raise ValueError("Unknown template. Use Fair Value, Momentum, or Late Settlement.")
            return {"reply": _md(meta["title"], [
                meta["summary"],
                f"When: {meta['when']}",
                f"Risk: {meta['risk']}",
            ])}
        if action == "summarize_market":
            snap = await self.engine.snapshot()
            market = snap.get("market") or {}
            feat = snap.get("features") or {}
            book = snap.get("book") or {}
            def _px(value):
                return "—" if value is None else f"{100 * float(value):.1f}¢"
            brti = feat.get("brti")
            target = market.get("target")
            return {"reply": _md(str(market.get("ticker") or "No live market"), [
                f"BRTI **{brti:,.2f}** · target **{target:,.2f}**"
                if isinstance(brti, (int, float)) and isinstance(target, (int, float))
                else f"BRTI **{brti or '—'}** · target **{target or '—'}**",
                f"**{(feat.get('seconds_left') or 0):.0f}s** left",
                f"YES {_px(book.get('yes_bid'))} / {_px(book.get('yes_ask'))}",
                f"NO {_px(book.get('no_bid'))} / {_px(book.get('no_ask'))}",
            ])}
        if action == "create_bot":
            kind = LEGACY_KINDS.get(command.get("template_id") or command.get("kind") or "", "fair-value")
            template = next((item for item in self.engine.catalog()["templates"]
                             if item["kind"] == kind), None)
            if not template:
                raise ValueError("Unknown bot template")
            slug = slugify(command.get("name") or "")
            if not slug:
                raise ValueError("What should we name it?")
            if command.get("budget") is None:
                raise ValueError("What paper budget should it start with?")
            result = await self.engine.create_agent(
                slug, template["kind"], float(command["budget"]),
                float(command["threshold"]) if command.get("threshold") is not None else template["threshold"],
                bool(command.get("deployed", True)), template["policy"])
            result["reply"] = _md(display_name(result["name"]), [
                f"Running **{template['title']}**",
                f"**${result['allocated_capital']:g}** simulated funds",
                "Deployed" if result.get("deployed") else "Created paused",
            ])
            return result
        if action == "pause_all":
            paused = []
            for exp in list(self.engine.experiments.values()):
                if exp.deployed:
                    await self.engine.control_agent(exp.name, "pause")
                    paused.append(exp.name)
            return {"reply": _md("Fleet", [
                "Paused " + ", ".join(f"**{display_name(n)}**" for n in paused)
                if paused else "Every bot is already paused."
            ])}
        if not name:
            raise ValueError("Which bot? Name one from the roster.")
        self.focus = name
        if action == "inspect_bot":
            return await self._inspect(name)
        if action == "why_not_traded":
            why = await self.engine.why_not_traded(name)
            if not why["counts"]:
                return {"name": name, "reply": _md(display_name(name), [
                    "No recent HOLD/WAIT decisions yet."]), "why_not": why}
            items = [f"**{row['label']}** — {row['n']}" for row in why["counts"]]
            return {"name": name, "why_not": why,
                    "reply": _md(f"{display_name(name)} — last 15 minutes", items)}
        if action == "summarize_bot":
            dossier = await self.engine.agent_dossier(name)
            stats = dossier.get("stats") or {}
            return {"name": name, "reply": _md(display_name(name), [
                f"**{dossier['guide']['title']}**",
                f"Cash **${dossier['cash']:.2f}** · P&L **{stats.get('net_pnl', 0):+.2f}**",
                f"{stats.get('settled', 0)} settled trades · owns {dossier.get('position')}",
                f"Last: {dossier.get('reason')}",
            ])}
        if action == "assign_template":
            kind = command.get("kind") or command.get("template_id")
            result = await self.engine.assign_job(name, kind=kind, template_id=command.get("template_id") or kind)
            result["reply"] = _md(display_name(name), [
                f"Now running **{result.get('job') or result.get('kind')}**",
                f"Min edge **{result['threshold']:.1%}**",
            ])
            return result
        if action == "add_cash":
            result = await self.engine.control_agent(
                name, "add_cash", delta=float(command.get("dollars") or command.get("delta") or 0))
            result["reply"] = _md(display_name(name), [
                f"Added paper cash. Cash is now **${result['cash']:.2f}**.",
            ])
            return result
        if action == "set_budget":
            result = await self.engine.control_agent(name, "budget", budget=float(command["budget"]))
            result["reply"] = _md(display_name(name), [
                f"Allocated budget is **${result['allocated_capital']:.2f}**.",
            ])
            return result
        if action == "set_threshold":
            if command.get("direction") and command.get("threshold") is None:
                step = -0.005 if command["direction"] == "more" else 0.005
                command["threshold"] = min(0.20, max(0.005, round(
                    self.engine.experiments[name].threshold + step, 3)))
            result = await self.engine.control_agent(
                name, "threshold", threshold=float(command["threshold"]))
            result["reply"] = _md(display_name(name), [
                f"Minimum edge is now **{result['threshold']:.1%}**.",
            ])
            return result
        if action == "set_beginner_settings":
            exp = self.engine.experiments[name]
            policy = dict(exp.policy)
            if command.get("max_order_dollars") is not None:
                policy["max_order_dollars"] = float(command["max_order_dollars"])
            if command.get("max_market_exposure") is not None:
                policy["max_market_exposure"] = float(command["max_market_exposure"])
                policy["max_total_exposure"] = max(policy["max_total_exposure"],
                                                   policy["max_market_exposure"])
            policy = normalize_policy(policy, self.engine.s)
            result = await self.engine.control_agent(name, "policy", policy=policy)
            if command.get("threshold") is not None:
                result = await self.engine.control_agent(
                    name, "threshold", threshold=float(command["threshold"]))
            result["reply"] = _md(display_name(name), ["Updated beginner settings."])
            return result
        if action == "update_advanced_settings":
            allowed = {field["id"] for field in ADVANCED_FIELDS}
            unknown = [key for key in command if key not in {"action", "name"} and key not in allowed]
            if unknown:
                raise ValueError(f"Unknown settings: {', '.join(unknown)}")
            exp = self.engine.experiments[name]
            policy = dict(exp.policy)
            changed = False
            for key in allowed:
                if command.get(key) is not None:
                    policy[key] = float(command[key])
                    changed = True
            if not changed:
                raise ValueError("No advanced settings were provided.")
            policy = normalize_policy(policy, self.engine.s)
            result = await self.engine.control_agent(name, "policy", policy=policy)
            result["reply"] = _md(display_name(name), ["Updated advanced settings."])
            return result
        if action == "deploy_bot":
            result = await self.engine.control_agent(name, "deploy")
            result["reply"] = _md(display_name(name), ["Deployed. It can buy again."])
            return result
        if action == "pause_bot":
            result = await self.engine.control_agent(name, "pause")
            result["reply"] = _md(display_name(name), [
                "Paused. Open positions still settle.",
            ])
            return result
        if action == "retire_bot":
            result = await self.engine.control_agent(name, "retire", flatten=True)
            result["reply"] = _md(display_name(name), ["Retired. History stays."])
            return result
        raise ValueError("FYFTEN does not have that tool.")


def _missing_args(command: dict[str, Any], names: list[str], focus: str | None) -> str | None:
    action = _alias(command.get("action") or "")
    if action in READ_TOOLS | {"summarize_best"} and action not in {
            "inspect_bot", "why_not_traded", "summarize_bot", "explain_template"}:
        return None
    if action == "create_bot":
        if not slugify(command.get("name") or ""):
            return "What should we name it?"
        if not command.get("template_id") and not command.get("kind"):
            return "Which template: Fair Value, Momentum, or Late Settlement?"
        if command.get("budget") is None:
            return "What paper budget should it start with?"
        return None
    if action == "explain_template" and not command.get("template_id") and not command.get("kind"):
        return "Which template should I explain?"
    if action == "pause_all":
        return None
    target = command.get("name") or (focus if focus in names else None)
    if action not in READ_TOOLS | {"create_bot", "pause_all", "list_templates"} and not target:
        return "Which bot on the roster?"
    if action == "add_cash" and not (command.get("dollars") or command.get("delta")):
        return "How many paper dollars should I add?"
    if action == "set_budget" and command.get("budget") is None:
        return "What should the allocated paper budget be?"
    if action == "set_threshold" and command.get("threshold") is None and not command.get("direction"):
        return "What leftover threshold, as a percent?"
    if action == "assign_template" and not command.get("kind") and not command.get("template_id"):
        return "Which template: Fair Value, Momentum, or Late Settlement?"
    return None


def compile_local(text: str, names: list[str], focus: str | None = None) -> dict[str, Any]:
    lowered = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not lowered:
        return {"error": "Type a command.", "help": HELP}

    if re.search(r"\b(what is an opportunity|templates?|fair value|late settlement)\b", lowered) and re.search(
            r"\b(explain|what is|show|list)\b", lowered):
        kind = _kind_from_text(lowered)
        if kind:
            return {"action": "explain_template", "template_id": kind}
        return {"action": "list_templates"}

    if re.search(r"\b(how many|list (the )?(agents|bots|roster)|who is (running|deployed))\b", lowered):
        return {"action": "list_bots"}
    if re.search(r"\bbest[- ]performing\b", lowered):
        return {"action": "summarize_best"}
    if re.search(r"\b(summarize|current) (the )?(market|contract)\b", lowered) or re.search(
            r"\bwhat('?s| is) the market\b", lowered):
        return {"action": "summarize_market"}
    if re.search(r"\bpause all\b", lowered):
        return {"action": "pause_all"}

    target = resolve_agent_name(lowered, names) or (focus if focus in names else None)
    if not target and len(names) == 1 and re.search(r"\b(him|her|this|that)\b", lowered):
        target = names[0]
    job = _kind_from_text(lowered)
    if job and target and re.search(
            r"\b(job|put|switch|assign|change|make him|make her|make this|template)\b", lowered):
        return {"action": "assign_template", "name": target, "kind": job, "template_id": job}

    create = re.search(
        r"\b(create|spin up|new)\b.*\b(bot|agent|fair|momentum|settlement|direction)\b",
        lowered)
    if create and not re.search(r"\b(add|give)\b.*\$", lowered):
        named = re.search(r"(?:named|called|name)\s+([a-z0-9][a-z0-9-]{1,31})", lowered)
        command = {"action": "create_bot", "budget": _money(lowered),
                   "deployed": not bool(re.search(r"\bpaused?\b", lowered)),
                   "name": named.group(1) if named else None,
                   "threshold": _cents(lowered)}
        if job:
            command["kind"] = job
            command["template_id"] = job
        return command

    if re.search(r"\b(retire|cash out|cash-out|cashout|shut down|remove)\b", lowered):
        if not target:
            return {"error": "Which bot should be cashed out and retired?"}
        return {"action": "retire_bot", "name": target}

    if re.search(r"\bwhy (didn'?t|did not|isn'?t|is not)\b", lowered) or re.search(
            r"\bwhy didn'?t .{0,40} trade\b", lowered):
        if not target:
            return {"error": "Which bot should I diagnose?"}
        return {"action": "why_not_traded", "name": target}

    if re.search(r"\b(go to|open|look at|inspect|check|use)\b", lowered) and target:
        return {"action": "inspect_bot", "name": target}

    if re.search(r"\b(more aggressive|less passive|too passive)\b", lowered):
        if not target:
            return {"error": "Which bot should be more aggressive?"}
        return {"action": "set_threshold", "name": target, "direction": "more"}
    if re.search(r"\b(less aggressive|more passive|too aggressive|pickier)\b", lowered):
        if not target:
            return {"error": "Which bot should be less aggressive?"}
        return {"action": "set_threshold", "name": target, "direction": "less"}

    if re.search(rf"\b(add|give)\b.*{MONEY_RE}", lowered) or re.search(
            rf"{MONEY_RE}\s+(more|extra)", lowered):
        if not target:
            return {"error": "Which bot should get the paper cash?"}
        return {"action": "add_cash", "name": target, "dollars": _money(lowered) or 20.0}

    if re.search(r"\b(pause|stop)\b", lowered) and target:
        return {"action": "pause_bot", "name": target}
    if re.search(r"\b(deploy|run|start|resume)\b", lowered) and target:
        return {"action": "deploy_bot", "name": target}

    if target and re.search(r"\b(edge|leftover|cash|balance|status|doing|own)\b", lowered):
        return {"action": "inspect_bot", "name": target}

    return {"error": HELP, "help": HELP}
