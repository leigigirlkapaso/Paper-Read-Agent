"""agent2/__init__.py"""
# read_paper kept as reference implementation only; production runs via
# pipeline._do_llm_read (handles caching + extraction persistence).
from .paper_reader import read_paper  # noqa: F401
from .parallel_runner import run_parallel

__all__ = ["run_parallel"]
