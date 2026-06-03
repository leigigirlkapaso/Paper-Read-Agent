"""
modules/thinker/constants.py
Thinker module shared constants.
"""

ROLE_USER = "用户"
ROLE_AI = "小思"

CONTENT_TYPE_INSIGHT = "insight"
CONTENT_TYPE_RESOLUTION = "resolution"
CONTENT_TYPE_CHAT_MESSAGE = "chat_message"

QUESTION_TYPE_INACTIVITY = "inactivity"
QUESTION_TYPE_RESOLUTION_FOLLOWUP = "resolution_followup"

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_CLOSED = "closed"

RESOLUTION_PENDING = "pending"
RESOLUTION_FULFILLED = "fulfilled"
RESOLUTION_ABANDONED = "abandoned"

INTENSITY_GENTLE = "gentle"
INTENSITY_MODERATE = "moderate"
INTENSITY_SHARP = "sharp"

DEFAULT_INTENSITY = INTENSITY_MODERATE
DEFAULT_MODE = "chat"

DT_NOW = "datetime('now')"

# 记忆类型
MEMORY_TYPE_INSIGHT = "insight"
MEMORY_TYPE_RESOLUTION = "resolution"
MEMORY_TYPE_PROFILE_SNAPSHOT = "profile_snapshot"
MEMORY_TYPE_SUMMARY = "summary"
MEMORY_TYPE_SPARK = "spark"

# 画像字段默认值
DEFAULT_PROFILE = {
    "research_domains": [],
    "knowledge_level": {},
    "thinking_style": "",
    "long_term_goals": [],
    "interaction_prefs": {"verbosity": "concise", "format": "bullet"},
}
