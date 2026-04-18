import json
import logging
from typing import Optional
from dataclasses import dataclass, field

log = logging.getLogger("doubao2api.sse")


@dataclass
class SSEEvent:
    """解析后的 SSE 事件"""
    event_type: str  # SSE_HEARTBEAT/SSE_ACK/FULL_MSG_NOTIFY/STREAM_MSG_NOTIFY/CHUNK_DELTA/STREAM_CHUNK/SSE_REPLY_END
    id: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class SessionMeta:
    """从 SSE 流中提取的会话元数据"""
    conversation_id: str = ""
    local_conversation_id: str = ""
    section_id: str = ""
    message_id: str = ""
    last_message_index: int = 0
    conversation_type: int = 0
    timeout_conf: dict = field(default_factory=dict)


@dataclass
class StreamResult:
    """SSE 流解析最终结果"""
    text: str = ""               # 完整文本内容
    session_meta: SessionMeta = field(default_factory=SessionMeta)
    suggestions: list = field(default_factory=list)  # 推荐问题
    brief: str = ""              # 消息摘要
    error: Optional[str] = None  # 错误信息
    image_urls: list = field(default_factory=list)  # 图片URL列表（文生图场景）


class DoubaoSSEParser:
    """豆包 SSE 流解析器"""

    def __init__(self):
        self.result = StreamResult()
        self._full_text = ""
        self._session_meta = SessionMeta()

    def parse_raw_sse(self, raw_body: str) -> StreamResult:
        """解析完整的 SSE 响应文本"""
        events = self._split_sse_events(raw_body)

        log.info(f"[SSE] Parsed {len(events)} events from {len(raw_body)} chars")
        if not events:
            # 诊断：raw_body 不像 SSE 格式
            if raw_body.strip().startswith("{"):
                log.warning(f"[SSE] raw_body 是 JSON 而非 SSE！preview: {raw_body[:300]}")
            elif raw_body.strip().startswith("<"):
                log.warning(f"[SSE] raw_body 是 HTML 而非 SSE！preview: {raw_body[:300]}")
            elif not raw_body.strip():
                log.warning("[SSE] raw_body 为空！")
            else:
                log.warning(f"[SSE] raw_body 格式异常！preview: {raw_body[:300]}")
        # # 打印事件类型摘要
        # evt_types = [e.event_type for e in events]
        # if evt_types:
        #     log.info(f"[SSE] Event types: {evt_types}")
        for evt in events:
            self._process_event(evt)

        self.result.text = self._full_text
        self.result.session_meta = self._session_meta
        return self.result

    def _split_sse_events(self, raw: str) -> list[SSEEvent]:
        """将原始 SSE 文本拆分为事件列表"""
        events = []
        current_id = ""
        current_event = ""
        current_data = ""

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                # 空行表示事件结束
                if current_event and current_data:
                    try:
                        data = json.loads(current_data) if current_data and current_data != "{}" else {}
                    except json.JSONDecodeError:
                        data = {}
                    events.append(SSEEvent(
                        event_type=current_event,
                        id=current_id,
                        data=data,
                    ))
                current_id = ""
                current_event = ""
                current_data = ""
                continue

            if line.startswith("id:"):
                current_id = line[3:].strip()
            elif line.startswith("event:"):
                current_event = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
                if current_data:
                    current_data += "\n" + data_str
                else:
                    current_data = data_str

        return events

    def _process_event(self, evt: SSEEvent):
        """处理单个 SSE 事件"""
        if evt.event_type == "SSE_HEARTBEAT":
            pass  # 心跳，忽略

        elif evt.event_type == "SSE_ACK":
            self._process_ack(evt.data)

        elif evt.event_type == "FULL_MSG_NOTIFY":
            pass  # 用户消息回显，忽略

        elif evt.event_type == "STREAM_MSG_NOTIFY":
            self._process_stream_msg(evt.data)

        elif evt.event_type == "CHUNK_DELTA":
            self._process_chunk_delta(evt.data)

        elif evt.event_type == "STREAM_CHUNK":
            self._process_stream_chunk(evt.data)

        elif evt.event_type == "SSE_REPLY_END":
            self._process_reply_end(evt.data)

        elif evt.event_type == "STREAM_ERROR":
            error_msg = evt.data.get("error_msg", "Unknown error")
            self.result.error = error_msg
            log.error(f"[SSE] STREAM_ERROR: {error_msg}")

    def _process_ack(self, data: dict):
        """处理 SSE_ACK 事件 - 提取会话元数据"""
        meta = data.get("ack_client_meta", {})
        conv_id = meta.get("conversation_id", "")
        if conv_id == "0":
            log.warning(f"[SSE] ⚠️ conversation_id='0' — 会话创建可能失败！ack_client_meta={json.dumps(meta, ensure_ascii=False)[:500]}")
        if not self._session_meta.conversation_id:
            self._session_meta.conversation_id = conv_id
        if not self._session_meta.section_id:
            self._session_meta.section_id = meta.get("section_id", "")
        self._session_meta.conversation_type = meta.get("conversation_type", 0)
        self._session_meta.local_conversation_id = meta.get("local_conversation_id", "")

        timeout = data.get("timeout_conf", {})
        if timeout:
            self._session_meta.timeout_conf = timeout

        # 提取用户消息 message_index
        query_list = data.get("query_list", [])
        if query_list:
            self._session_meta.last_message_index = query_list[0].get("message_index", 0)

    def _process_stream_msg(self, data: dict):
        """处理 STREAM_MSG_NOTIFY 事件 - 首个文本块 + 元数据"""
        meta = data.get("meta", {})
        if not self._session_meta.message_id:
            self._session_meta.message_id = meta.get("message_id", "")
        if not self._session_meta.section_id:
            self._session_meta.section_id = meta.get("section_id", "")

        # 提取机器人回复的 index_in_conv
        idx = meta.get("index_in_conv")
        if idx is not None:
            self._session_meta.last_message_index = idx

        # 提取首个文本块
        content = data.get("content", {})
        blocks = content.get("content_block", [])
        
        # 诊断：打印首个 STREAM_MSG_NOTIFY 的完整 content 结构
        if not DoubaoSSEParser._stream_msg_logged:
            DoubaoSSEParser._stream_msg_logged = True
            log.info(f"[SSE] 首个 STREAM_MSG_NOTIFY content keys={list(content.keys())}")
            log.info(f"[SSE]   content_block 数量={len(blocks)}")
            for i, block in enumerate(blocks[:3]):
                block_type = block.get("block_type", 0)
                block_content = block.get("content", {})
                block_content_json = json.dumps(block_content, ensure_ascii=False)[:800]
                log.info(f"[SSE]   block[{i}]: block_type={block_type}, content={block_content_json}")

        for block in blocks:
            if block.get("block_type") == 10000:
                text = block.get("content", {}).get("text_block", {}).get("text", "")
                if text:
                    self._full_text += text
                break

        # 检测文生图 Agent
        ext = content.get("ext", {})
        bot_state = ext.get("bot_state", "")
        if "Agent-Text2Image" in str(bot_state):
            log.info("[SSE] Image generation agent detected")

    def _process_chunk_delta(self, data: dict):
        """处理 CHUNK_DELTA 事件 - 增量文本（最简洁路径）"""
        text = data.get("text", "")
        if text:
            self._full_text += text

    _first_stream_chunk_logged = 0  # 类变量，控制诊断打印前N个
    _stream_msg_logged = False

    def _process_stream_chunk(self, data: dict):
        """处理 STREAM_CHUNK 事件 - 补丁更新"""
        patch_ops = data.get("patch_op", [])

        # 诊断：打印前3个 STREAM_CHUNK 的完整结构
        if DoubaoSSEParser._first_stream_chunk_logged < 3:
            DoubaoSSEParser._first_stream_chunk_logged += 1
            log.info(f"[SSE] STREAM_CHUNK #{DoubaoSSEParser._first_stream_chunk_logged} data keys={list(data.keys())}")
            for i, op in enumerate(patch_ops):
                po = op.get("patch_object", 0)
                pv = op.get("patch_value", {})
                pv_json = json.dumps(pv, ensure_ascii=False) if isinstance(pv, dict) else str(pv)[:500]
                log.info(f"[SSE]   patch_op[{i}]: patch_object={po}, patch_value={pv_json[:1000]}")

        for op in patch_ops:
            patch_object = op.get("patch_object", 0)

            if patch_object == 1:
                # content_block 更新（可能包含文本 block_type=10000 或文生图 block_type=2074）
                patches = op.get("patch_value", {}).get("content_block", [])
                for block in patches:
                    block_type = block.get("block_type", 0)
                    if block_type == 2074:
                        # 文生图 creation_block
                        self._extract_image_urls(block)
                    elif block_type == 10000:
                        # 文本块 — 提取增量文本
                        text = block.get("content", {}).get("text_block", {}).get("text", "")
                        if text:
                            self._full_text += text
                    elif block_type == 10101:
                        # loading_block — 忽略
                        pass
                    else:
                        # 未知 block_type — 记录一次
                        log.debug(f"[SSE] 未知 block_type={block_type} in STREAM_CHUNK")

            elif patch_object == 3:
                # 完成标记
                pv = op.get("patch_value", {})
                # 可能包含 msg_finish_attr / answer_finish_attr
                msg_attr = pv.get("msg_finish_attr", {})
                if msg_attr and msg_attr.get("brief") and not self.result.brief:
                    self.result.brief = msg_attr["brief"]
                answer_attr = pv.get("answer_finish_attr", {})
                if answer_attr:
                    pass  # has_suggest 等信息

            elif patch_object == 102:
                # 增量文本（游客模式 / 部分场景的文本路径）
                # content 是 JSON 字符串：{"text": "你好", "text_tags": []}
                pv = op.get("patch_value", {})
                content_str = pv.get("content", "")
                if content_str:
                    try:
                        content_obj = json.loads(content_str) if isinstance(content_str, str) else content_str
                        text = content_obj.get("text", "")
                        if text:
                            self._full_text += text
                    except (json.JSONDecodeError, AttributeError):
                        # 非标准 JSON，尝试直接取文本
                        if isinstance(content_str, str) and content_str:
                            self._full_text += content_str

            elif patch_object == 50:
                # ext 元数据更新 - 可能包含建议
                ext = op.get("patch_value", {}).get("ext", {})
                sp_v2 = ext.get("sp_v2", "")
                if sp_v2:
                    try:
                        suggestions = json.loads(sp_v2)
                        if isinstance(suggestions, list):
                            self.result.suggestions = suggestions
                    except json.JSONDecodeError:
                        pass

    def _extract_image_urls(self, block: dict):
        """从 creation_block 提取图片 URL"""
        content = block.get("content", {})
        creation_block = content.get("creation_block", {})
        creations = creation_block.get("creations", [])
        for creation in creations:
            image = creation.get("image", {})
            # 优先取无水印原图
            url = image.get("image_ori_raw", {}).get("url", "")
            if not url:
                url = image.get("image_ori", {}).get("url", "")
            if not url:
                url = image.get("image_url", "")
            if url and url not in self.result.image_urls:
                self.result.image_urls.append(url)

    def _process_reply_end(self, data: dict):
        """处理 SSE_REPLY_END 事件"""
        end_type = data.get("end_type", 0)

        if end_type == 1:
            # 消息完成 - 提取 brief
            msg_attr = data.get("msg_finish_attr", {})
            self.result.brief = msg_attr.get("brief", "")

        elif end_type == 2:
            # 回答完成 - has_suggest
            answer_attr = data.get("answer_finish_attr", {})
            # suggestions 可能通过后续 STREAM_CHUNK 返回

        # end_type >= 1 即表示流结束
