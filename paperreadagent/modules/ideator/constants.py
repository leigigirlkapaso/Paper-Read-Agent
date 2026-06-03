"""
modules/ideator/constants.py
"""

# 火花状态
SPARK_SEED = "seed"
SPARK_DEEPENING = "deepening"
SPARK_DEEP_DONE = "deep_done"

# 来源类型
SOURCE_CROSS_PROJECT = "cross_project"
SOURCE_CROSS_LAYER = "cross_layer"
SOURCE_CONTRADICTION = "contradiction"
SOURCE_RANDOM = "random"
SOURCE_TIMELINE = "timeline"

# 关联类型
LINK_SIMILARITY = "similarity"
LINK_CONTRADICTION = "contradiction"
LINK_TEMPORAL = "temporal"
LINK_RANDOM = "random"
LINK_CROSS_LAYER = "cross_layer"
LINK_CROSS_PROJECT = "cross_project"
LINK_RANDOM_WALK = "random_walk"
LINK_TIMELINE = "timeline"

# 火花分级模型
DEFAULT_CROSS_SCORER_MODEL = "haiku"
DEFAULT_SPARK_GENERATOR_MODEL = "gpt-4o"

# 去重阈值
DEDUP_MERGE_THRESHOLD = 0.85
DEDUP_FLAG_THRESHOLD = 0.6

# 质量衰减
QUALITY_USEFUL_DELTA = 0.2
QUALITY_BAD_DELTA = -0.3
QUALITY_GC_THRESHOLD = 0.15
QUALITY_GC_AGE_DAYS = 7
