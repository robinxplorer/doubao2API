import logging
import time
import uuid
from typing import Optional
from dataclasses import dataclass, field

log = logging.getLogger("doubao2api.session")


@dataclass
class DoubaoSession:
    """豆包会话状态"""
    session_id: str = ""         # 本地会话标识
    conversation_id: str = ""    # 豆包 conversation_id
    local_conversation_id: str = ""  # 本地会话ID（local_xxx）
    section_id: str = ""         # section_id
    last_message_index: int = 0  # 消息序号
    bot_id: str = ""             # 当前 bot_id
    turn_count: int = 0          # 当前会话已轮次
    created_at: float = 0.0      # 创建时间


class SessionStore:
    """会话状态管理器"""

    def __init__(self):
        self._sessions: dict[str, DoubaoSession] = {}  # session_id → DoubaoSession

    def create_session(self, bot_id: str, conversation_id: str = "") -> DoubaoSession:
        """创建新会话"""
        session = DoubaoSession(
            session_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            local_conversation_id=f"local_{int(time.time() * 1000)}{uuid.uuid4().hex[:6]}",
            section_id="",
            last_message_index=0,  # 新会话传 null，这里用 0
            bot_id=bot_id,
            turn_count=0,
            created_at=time.time(),
        )
        self._sessions[session.session_id] = session
        log.info(f"[Session] Created session {session.session_id[:8]}... bot_id={bot_id}")
        return session

    def get_session(self, session_id: str) -> Optional[DoubaoSession]:
        return self._sessions.get(session_id)

    def update_from_sse(self, session_id: str, conversation_id: str = None,
                        section_id: str = None, message_index: int = None):
        """从 SSE 响应更新会话状态"""
        session = self._sessions.get(session_id)
        if not session:
            return

        if conversation_id:
            session.conversation_id = conversation_id
        if section_id:
            session.section_id = section_id
        if message_index is not None:
            session.last_message_index = message_index

    def build_client_meta(self, session_id: str) -> dict:
        """构建请求的 client_meta"""
        session = self._sessions.get(session_id)
        if not session:
            return {}

        is_new = not session.conversation_id

        if is_new:
            return {
                "local_conversation_id": session.local_conversation_id,
                "conversation_id": "",
                "bot_id": session.bot_id,
                "last_section_id": "",
                "last_message_index": None,
            }
        else:
            return {
                "local_conversation_id": session.local_conversation_id,
                "conversation_id": session.conversation_id,
                "bot_id": session.bot_id,
                "last_section_id": session.section_id,
                "last_message_index": session.last_message_index,
            }

    def build_option(self, session_id: str) -> dict:
        """构建请求的 option"""
        session = self._sessions.get(session_id)
        is_new = not session.conversation_id if session else True
        now_ms = int(time.time() * 1000)

        return {
            "send_message_scene": "",
            "create_time_ms": now_ms,
            "collect_id": "",
            "is_audio": False,
            "answer_with_suggest": False,
            "tts_switch": False,
            "need_deep_think": 0,
            "click_clear_context": False,
            "from_suggest": False,
            "is_regen": False,
            "is_replace": False,
            "disable_sse_cache": False,
            "select_text_action": "",
            "resend_for_regen": False,
            "scene_type": 0,
            "unique_key": str(uuid.uuid4()),
            "start_seq": 0,
            "need_create_conversation": is_new,
            "regen_query_id": [],
            "edit_query_id": [],
            "regen_instruction": "",
            "no_replace_for_regen": False,
            "message_from": 0,
            "shared_app_name": "",
            "sse_recv_event_options": {"support_chunk_delta": True},
            "is_ai_playground": False,
            "recovery_option": {
                "is_recovery": False,
                "req_create_time_sec": int(time.time()),
            },
        }

    def build_message(self, text: str) -> dict:
        """构建消息体"""
        return {
            "local_message_id": str(uuid.uuid4()),
            "content_block": [{
                "block_type": 10000,
                "content": {
                    "text_block": {
                        "text": text,
                        "icon_url": "",
                        "icon_url_dark": "",
                        "summary": "",
                    },
                    "pc_event_block": "",
                },
                "block_id": str(uuid.uuid4()),
                "parent_id": "",
                "meta_info": [],
                "append_fields": [],
            }],
            "message_status": 0,
        }

    def build_ext(self) -> dict:
        """构建 ext 字段"""
        from backend.core.config import settings
        return {
            "use_deep_think": "0",
            "fp": settings.DEFAULT_FP,
            "commerce_credit_config_enable": "0",
            "sub_conv_firstmet_type": "0",
        }

    def build_full_payload(self, session_id: str, text: str) -> dict:
        """构建完整的请求 payload"""
        return {
            "client_meta": self.build_client_meta(session_id),
            "messages": [self.build_message(text)],
            "option": self.build_option(session_id),
            "ext": self.build_ext(),
        }

    def increment_turn(self, session_id: str):
        """增加轮次计数"""
        session = self._sessions.get(session_id)
        if session:
            session.turn_count += 1

    def remove_session(self, session_id: str):
        """移除会话"""
        self._sessions.pop(session_id, None)

    def status(self) -> dict:
        return {
            "active_sessions": len(self._sessions),
        }
