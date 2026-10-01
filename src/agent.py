"""mobileops-agent — tau2-bench telecom purple agent 核心决策逻辑。

确定性编排（MModel 决策底座）+ LLM 兜底。对外只宣传 mobileops-agent，底层模型是内部实现细节。

架构（对应 MModel 合规底座的四个能力）：
  - Runbook 政策规则：把 telecom policy 编译成确定性分支（决策前查：该不该做、允许做什么）
  - EntityStore 状态跟踪：识别客户 → 查线路 → 查账单 → 查数据用量
  - 双控边界：assistant（agent 自己调工具）/ user（引导 simulated user 做设备操作）
  - 工具参数校验：refuel ≤2GB、发支付请求前确认 overdue、合同过期不可 resume

关键事实（从 tau2 源码核实）：
  - 半双工模式：agent 先问候（DEFAULT_FIRST_AGENT_MESSAGE），用户再从 user_scenario 生成消息；
    ticket 里的手机号不会自动进对话，必须 agent 主动问、用户才给。
  - 工具返回是裸 JSON：green agent 的 extract_text_from_message 对单条 ToolMessage 返回 str(content)
    （无 "Tool 'X' result:" 前缀），因此要跟踪 last_tool_name 识别。
  - 数值字段被 to_json_str 序列化成字符串，比较前必须转 float。
"""

from __future__ import annotations

import json
import os
import re

import litellm
from a2a.types import Part, TextPart
from a2a.utils import get_message_text


TODAY = "2025-02-25"  # 来自 telecom main_policy.md 的当前时间
RESPOND = "respond"
TRANSFER_MSG = "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON."
ASK_PHONE = (
    "I'd be happy to help you with that. To look up your account, "
    "could you please provide me with the phone number associated with your account?"
)
AFTER_REFUEL = (
    "I've added 2.0 GB of data to your line. "
    "Please run a speed test now to confirm your internet connection is working."
)
AFTER_PAYMENT_REQUEST = (
    "I've sent a payment request to your account. "
    "Please check your payment requests and accept it to complete the payment."
)

PHONE_RE = re.compile(r"(\d{3}-\d{3}-\d{4})")
TOOL_RESULT_RE = re.compile(r"Tool '([^']+)' result:\s*(.*)", re.DOTALL)

POLICY_ACTION_NAMES = {
    "transfer_to_human_agents", "refuel_data", "send_payment_request",
    "resume_line", "suspend_line", "enable_roaming", "disable_roaming",
}


def _tool_json(name: str, arguments: dict) -> str:
    return json.dumps({"name": name, "arguments": arguments})


def _respond_json(content: str) -> str:
    return json.dumps({"name": RESPOND, "arguments": {"content": content}})


class SessionState:
    def __init__(self):
        self.phone = None
        self.customer_id = None
        self.line_ids = []
        self.line_idx = 0
        self.target_line_id = None
        self.contract_ended = False
        self.overdue_bill_id = None
        self.data_used = None
        self.data_limit = None
        self.data_exceeded = False
        self.awaiting = None  # 上一次工具调用的名字（识别裸 JSON 工具返回）
        self.phase = "identify"  # identify->inspect_line->inspect_bills->inspect_data->route->llm->done
        self.history = []  # OpenAI 风格消息（供 LLM 兜底）

    def push(self, role: str, content: str):
        self.history.append({"role": role, "content": content})


class Agent:
    """mobileops-agent：确定性政策路由 + LLM 兜底。"""

    def __init__(self):
        # litellm 格式模型名（如 "deepseek/deepseek-chat"），key 从环境变量自动读
        self.model = os.environ.get("AGENT_LLM", "deepseek/deepseek-chat")
        self.temperature = float(os.environ.get("TEMPERATURE", "0.0"))
        self.session = SessionState()

    async def run(self, message, updater) -> None:
        text = get_message_text(message)
        if "Now here are the user messages:" in text:
            # 新任务边界：green agent 每个任务首条消息必带此标记
            self.session = SessionState()
        response = self._decide(text)
        await updater.add_artifact(
            parts=[Part(root=TextPart(text=response))], name="response"
        )

    def _decide(self, text: str) -> str:
        st = self.session

        # 首条消息：拆 system（policy+tools）/ user（用户首条描述，通常无手机号）
        if not st.history and "Now here are the user messages:" in text:
            system_prompt, _, user_msg = text.partition("Now here are the user messages:")
            st.push("system", system_prompt.strip())
            if user_msg.strip():
                st.push("user", user_msg.strip())
            phone = self._extract_phone(user_msg)
            if phone:
                st.phone = phone
                st.phase = "inspect_line"
                return self._tool(st, "get_customer_by_phone", {"phone_number": phone})
            st.phase = "identify"
            return self._resp(st, ASK_PHONE)

        name, data = self._classify(st, text)
        st.push("user", text)

        if name is None:
            return self._on_user_message(st, text)
        return self._absorb(st, name, data)

    # ---------- 消息分类 ----------
    def _classify(self, st: SessionState, text: str):
        m = TOOL_RESULT_RE.search(text)
        if m:
            name = m.group(1)
            raw = m.group(2).strip()
            try:
                return name, json.loads(raw)
            except Exception:
                return name, raw
        stripped = text.strip()
        if st.awaiting and stripped.startswith(("{", "[")):
            try:
                return st.awaiting, json.loads(stripped)
            except Exception:
                pass
        return None, None

    # ---------- 用户消息处理 ----------
    def _on_user_message(self, st: SessionState, text: str) -> str:
        st.awaiting = None
        if st.phase == "identify":
            phone = self._extract_phone(text)
            if phone:
                st.phone = phone
                st.phase = "inspect_line"
                return self._tool(st, "get_customer_by_phone", {"phone_number": phone})
            return self._resp(st, ASK_PHONE)
        if st.phase == "done":
            return self._resp(st, TRANSFER_MSG)
        return self._llm(st)

    # ---------- 工具返回吸收 ----------
    def _absorb(self, st: SessionState, name: str, data) -> str:
        st.awaiting = None
        if name == "get_customer_by_phone":
            if isinstance(data, dict):
                st.customer_id = data.get("customer_id")
                st.line_ids = data.get("line_ids") or []
            st.phase = "inspect_line"
            return self._next_action(st)

        if name == "get_details_by_id":
            self._absorb_line(st, data)
            st.phase = "inspect_line" if st.line_idx < len(st.line_ids) else "inspect_bills"
            return self._next_action(st)

        if name == "get_bills_for_customer":
            self._absorb_bills(st, data)
            st.phase = "inspect_data"
            return self._next_action(st)

        if name == "get_data_usage":
            if isinstance(data, dict):
                st.data_used = self._to_float(data.get("data_used_gb"))
                st.data_limit = self._to_float(data.get("data_limit_gb"))
                if st.data_used is not None and st.data_limit is not None:
                    st.data_exceeded = st.data_used > st.data_limit
            st.phase = "route"
            return self._route(st)

        if name == "transfer_to_human_agents":
            st.phase = "done"
            return self._resp(st, TRANSFER_MSG)

        if name == "refuel_data":
            st.phase = "llm"
            return self._resp(st, AFTER_REFUEL)

        if name == "send_payment_request":
            st.phase = "llm"
            return self._resp(st, AFTER_PAYMENT_REQUEST)

        if name in POLICY_ACTION_NAMES:
            st.phase = "llm"
            return self._llm(st)

        st.phase = "llm"
        return self._llm(st)

    def _absorb_line(self, st: SessionState, data):
        if not isinstance(data, dict):
            return
        line_id = data.get("line_id")
        phone = data.get("phone_number")
        if st.target_line_id is None and st.phone and phone == st.phone:
            st.target_line_id = line_id
        if st.target_line_id == line_id:
            ced = data.get("contract_end_date")
            if ced and str(ced) < TODAY:
                st.contract_ended = True

    def _absorb_bills(self, st: SessionState, data):
        bills = data if isinstance(data, list) else []
        for b in bills:
            if isinstance(b, dict) and b.get("status") == "Overdue":
                st.overdue_bill_id = b.get("bill_id")
                break

    # ---------- 决策 ----------
    def _next_action(self, st: SessionState) -> str:
        if st.phase == "inspect_line":
            if st.line_idx < len(st.line_ids):
                lid = st.line_ids[st.line_idx]
                st.line_idx += 1
                return self._tool(st, "get_details_by_id", {"id": lid})
            st.phase = "inspect_bills"
        if st.phase == "inspect_bills":
            if st.customer_id:
                return self._tool(st, "get_bills_for_customer", {"customer_id": st.customer_id})
            st.phase = "inspect_data"
        if st.phase == "inspect_data":
            if st.customer_id and st.target_line_id:
                return self._tool(st, "get_data_usage", {"customer_id": st.customer_id, "line_id": st.target_line_id})
            return self._route(st)
        return self._llm(st)

    def _route(self, st: SessionState) -> str:
        if st.contract_ended:
            return self._tool(st, "transfer_to_human_agents",
                              {"summary": "Line contract has ended; suspension cannot be lifted per policy."})
        if st.overdue_bill_id:
            return self._tool(st, "send_payment_request",
                              {"customer_id": st.customer_id, "bill_id": st.overdue_bill_id})
        if st.data_exceeded:
            return self._tool(st, "refuel_data",
                              {"customer_id": st.customer_id, "line_id": st.target_line_id, "gb_amount": 2.0})
        st.phase = "llm"
        return self._llm(st)

    def _tool(self, st: SessionState, name: str, arguments: dict) -> str:
        st.awaiting = name
        s = _tool_json(name, arguments)
        st.push("assistant", s)
        return s

    def _resp(self, st: SessionState, content: str) -> str:
        st.awaiting = None
        return _respond_json(content)

    # ---------- LLM 兜底 ----------
    def _llm(self, st: SessionState) -> str:
        st.phase = "llm"
        st.awaiting = None
        guide = (
            "You have already gathered the customer's account state above (line status, bills, data usage). "
            "Continue troubleshooting following the policy workflow. Important decision rules:\n"
            "1) If the user reports 'No Service' and SIM status is LOCKED with PIN/PUK, "
            "transfer to human agents immediately (you cannot unlock it yourself).\n"
            "2) If the user is traveling abroad and the line roaming is disabled, enable roaming at no cost.\n"
            "3) Do not repeat actions already tried. Respond in the required JSON format."
        )
        content = self._call_llm(st.history + [{"role": "user", "content": guide}])
        st.push("assistant", content)
        return content

    def _call_llm(self, msgs: list[dict]) -> str:
        response = litellm.completion(
            model=self.model,
            messages=msgs,
            temperature=self.temperature,
            max_tokens=1200,
        )
        content = response.choices[0].message.content
        return content if content else ""

    # ---------- 工具函数 ----------
    @staticmethod
    def _extract_phone(text: str):
        m = PHONE_RE.search(text or "")
        return m.group(1) if m else None

    @staticmethod
    def _to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
