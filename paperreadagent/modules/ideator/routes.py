"""
modules/ideator/routes.py
"""

from __future__ import annotations

import asyncio
import json
import logging

from pathlib import Path

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader

from .project_brief import ProjectBriefService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="")

_template_dir = Path(__file__).parent / "templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_template_dir)))


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    is_mobile = getattr(request.state, "is_mobile", False)
    tmpl = _jinja_env.get_template("dashboard.html")
    return tmpl.render(is_mobile=is_mobile)


@router.get("/roundtable/{rt_id}", response_class=HTMLResponse)
async def roundtable_page(request: Request, rt_id: int):
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    rt = data.get_roundtable(rt_id)
    spark_id = rt["spark_id"] if rt else 0
    tmpl = _jinja_env.get_template("roundtable.html")
    return tmpl.render(rt_id=rt_id, spark_id=spark_id)


@router.get("/api/sparks")
async def list_sparks(
    request: Request,
    status: str | None = None,
    source_type: str | None = None,
):
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    sparks = data.list_sparks(status=status, source_type=source_type)
    for s in sparks:
        try:
            s["source_refs"] = json.loads(s.get("source_refs", "[]"))
        except (json.JSONDecodeError, TypeError):
            s["source_refs"] = []
        try:
            s["metadata"] = json.loads(s.get("metadata", "{}"))
        except (json.JSONDecodeError, TypeError):
            s["metadata"] = {}
        # Resolve source titles
        source_titles = []
        for ref in s["source_refs"]:
            if isinstance(ref, dict):
                if ref.get("type") == "paper":
                    try:
                        paper = data.get_paper(ref["id"])
                        if paper:
                            source_titles.append(paper.get("title", f"论文#{ref['id']}"))
                    except Exception:
                        logger.warning("[ideator] paper title lookup failed", exc_info=True)  # paper lookup — non-critical
                elif ref.get("type") == "core_note":
                    source_titles.append(f"笔记#{ref['id']}")
        s["source_titles"] = source_titles
    return JSONResponse(sparks)


@router.post("/api/sparks/{spark_id}/deepen")
async def deepen_spark(request: Request, spark_id: int):
    core = request.app.state.core
    from .data_access import DataAccess
    from .pipeline import IdeatorPipeline
    data = DataAccess(core)
    pipeline = IdeatorPipeline(core, data)
    result = await pipeline.deepen(spark_id)
    if result is None:
        return JSONResponse({"error": "深化失败"}, status_code=500)
    return JSONResponse({"spark_id": spark_id, "depth_content": result})


@router.post("/api/sparks/{spark_id}/project-brief")
async def generate_project_brief(request: Request, spark_id: int):
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    svc = ProjectBriefService(core, data)
    brief_id = await svc.generate(spark_id)
    brief = data.get_project_brief(brief_id)
    return {"brief_id": brief_id, "status": brief["status"] if brief else "failed"}


@router.get("/api/sparks/{spark_id}/project-briefs")
async def list_project_briefs(request: Request, spark_id: int):
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    briefs = data.list_project_briefs(spark_id)
    return {"spark_id": spark_id, "briefs": briefs}


@router.get("/api/project-briefs/{brief_id}")
async def get_project_brief(request: Request, brief_id: int):
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    brief = data.get_project_brief(brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="project brief not found")
    return brief


@router.get("/api/sparks/{spark_id}/reviews")
async def get_spark_reviews(request: Request, spark_id: int):
    """Get all review records for a spark"""
    core = request.app.state.core
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_review_records WHERE spark_id = ? ORDER BY created_at",
        (spark_id,)
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/sparks/{spark_id}/detail")
async def get_spark_detail(request: Request, spark_id: int):
    """获取火花完整详情：简报、辩论记录、草稿、审查记录"""
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)

    spark = data.get_spark(spark_id)
    if not spark:
        return JSONResponse({"error": "Spark not found"}, status_code=404)

    # Parse JSON fields
    try:
        spark["source_refs"] = json.loads(spark.get("source_refs", "[]"))
    except (json.JSONDecodeError, TypeError):
        spark["source_refs"] = []
    try:
        spark["metadata"] = json.loads(spark.get("metadata", "{}"))
    except (json.JSONDecodeError, TypeError):
        spark["metadata"] = {}

    # Source titles
    source_titles = []
    for ref in spark["source_refs"]:
        if isinstance(ref, dict):
            if ref.get("type") == "paper":
                try:
                    paper = data.get_paper(ref["id"])
                    if paper:
                        source_titles.append({
                            "id": ref["id"],
                            "title": paper.get("title", ""),
                            "type": "paper",
                        })
                except Exception:
                    logger.warning("[ideator] paper title lookup failed in detail view", exc_info=True)  # paper lookup — non-critical
            elif ref.get("type") == "core_note":
                source_titles.append({
                    "id": ref["id"],
                    "title": f"笔记#{ref['id']}",
                    "type": "core_note",
                })
    spark["source_titles"] = source_titles

    # S3 briefing + debate records from metadata
    meta = spark.get("metadata", {})
    spark["s3_briefing"] = meta.get("s3_briefing")
    spark["debate_summary"] = meta.get("debate_summary")
    spark["debate_rounds"] = meta.get("debate_rounds", [])

    # Review records
    rows = core.db.conn.execute(
        "SELECT * FROM ideator_review_records WHERE spark_id = ? ORDER BY created_at",
        (spark_id,),
    ).fetchall()
    spark["review_records"] = [dict(r) for r in rows]

    # Roundtable briefing & messages
    rt_rows = core.db.conn.execute(
        "SELECT id FROM ideator_roundtables WHERE spark_id = ? ORDER BY id DESC LIMIT 1",
        (spark_id,),
    ).fetchall()
    if rt_rows:
        rt_id = rt_rows[0][0]
        rt_msgs = core.db.conn.execute(
            "SELECT * FROM ideator_roundtable_messages WHERE roundtable_id = ? ORDER BY created_at",
            (rt_id,),
        ).fetchall()
        spark["roundtable_messages"] = [dict(r) for r in rt_msgs]
        # Roundtable briefing from team_memory
        tm_rows = core.db.conn.execute(
            "SELECT content FROM ideator_team_memory WHERE spark_id = ? AND memory_type = 'watermark' ORDER BY created_at DESC LIMIT 1",
            (spark_id,),
        ).fetchall()
        if tm_rows:
            spark["roundtable_briefing"] = tm_rows[0][0]

    return JSONResponse(spark)


@router.get("/api/runs")
async def list_runs(request: Request, limit: int = 20):
    """List recent pipeline runs"""
    core = request.app.state.core
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_pipeline_runs ORDER BY started_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/weights")
async def get_weights(request: Request):
    """Get recall path weights"""
    core = request.app.state.core
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_recall_weights ORDER BY weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@router.post("/api/sparks/{spark_id}/feedback")
async def feedback_spark(
    request: Request, spark_id: int, feedback: str = Form(...),
):
    core = request.app.state.core
    from .data_access import DataAccess
    from .spark_store import SparkStore
    from .feedback_loop import FeedbackLoop
    data = DataAccess(core)
    store = SparkStore(data)
    store.apply_feedback(spark_id, feedback)

    # Record feedback in FeedbackLoop to adjust recall path weights
    loop = FeedbackLoop(data)
    spark = data.get_spark(spark_id)
    if spark:
        source_type = spark.get("source_type", "")
        if source_type:
            loop.record_feedback(source_type, feedback)

    return JSONResponse({"ok": True})


@router.post("/api/mine")
async def trigger_mine(request: Request, scope: str = Form(default="all")):
    core = request.app.state.core
    from .data_access import DataAccess
    from .pipeline import IdeatorPipeline
    data = DataAccess(core)
    pipeline = IdeatorPipeline(core, data)
    result = await pipeline.run_full_with_diag(scope=scope)
    return JSONResponse(result)


# ── 圆桌讨论 API ──────────────────────────────────────────────

from pydantic import BaseModel


class AskRoundRequest(BaseModel):
    question: str
    mentioned: list[str] = ["all"]


class SupplementRequest(BaseModel):
    seat_id: str
    content: str


class DirectRoundRequest(BaseModel):
    content: str


@router.post("/api/roundtable/direct")
async def start_direct_roundtable(request: Request, body: DirectRoundRequest):
    """直接发起圆桌：用户输入研究内容，gen 先回应，6 坐席全保留"""
    core = request.app.state.core
    from .data_access import DataAccess
    from . import get_roundtable_manager
    data = DataAccess(core)
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    if not body.content.strip():
        return JSONResponse({"error": "内容不能为空"}, status_code=400)

    rt_id = mgr.create_team(
        spark_id=0,
        spark_content="",
        source_refs=[],
        spark_content_override=body.content.strip(),
    )

    # 首轮：用户内容发给 gen，并持久化消息
    team = mgr.get_team(rt_id)
    if team:
        try:
            results = await team.start_round(question=body.content.strip(), mentioned=["gen"])
            for msg in results:
                msg_copy = dict(msg)
                msg_copy["roundtable_id"] = rt_id
                data.insert_roundtable_message(**msg_copy)
            data.update_roundtable(rt_id, round_count=team.round_number)
        except Exception:
            logger.warning("[ideator] direct roundtable initial round failed", exc_info=True)

    return JSONResponse({"roundtable_id": rt_id, "status": "active"})


@router.post("/api/sparks/{spark_id}/roundtable/start")
async def start_roundtable(request: Request, spark_id: int):
    """发起圆桌讨论"""
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    spark = data.get_spark(spark_id)
    if not spark:
        return JSONResponse({"error": "Spark not found"}, status_code=404)
    from paperreadagent.utils.json_utils import safe_json_loads
    source_refs = safe_json_loads(spark.get("source_refs", "[]"), default=[])
    rt_id = mgr.create_team(
        spark_id=spark_id,
        spark_content=spark.get("content", ""),
        source_refs=source_refs,
    )
    return JSONResponse({"roundtable_id": rt_id, "status": "active"})


@router.get("/api/roundtables/{rt_id}")
async def get_roundtable(request: Request, rt_id: int):
    """获取圆桌状态"""
    core = request.app.state.core
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    seat_status = [
        {"seat_id": s.seat_id, "role": s.role,
         "quota": s.quota, "remaining": s.remaining_quota,
         "state": s.state}
        for s in team.seats.values()
    ]
    hot_pct = 0.0
    warm_pct = 0.0
    if team._graduation:
        hot_layer = team._graduation.layers.get("hot")
        warm_layer = team._graduation.layers.get("warm")
        hot_pct = hot_layer.pct if hot_layer else 0.0
        warm_pct = warm_layer.pct if warm_layer else 0.0
    return JSONResponse({
        "roundtable_id": rt_id,
        "spark_id": team.spark_id,
        "round_number": team.round_number,
        "messages": team.messages[-50:],
        "seats": seat_status,
        "watermark": {"hot_pct": hot_pct, "warm_pct": warm_pct},
    })


@router.get("/api/roundtables/{rt_id}/stream")
async def stream_roundtable(request: Request, rt_id: int):
    """SSE stream of agent token chunks for roundtable rt_id.

    Page-lifecycle: client opens this on page enter, server keeps it open
    until client closes (page nav/refresh) or close_rt is called by manager.
    Emits keepalive SSE comments every 15s to defeat proxy idle timeouts.
    """
    import json as _json
    from fastapi.responses import StreamingResponse
    from . import get_roundtable_manager
    from .stream_hub import get_stream_hub

    mgr = get_roundtable_manager()
    if mgr is None:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    if mgr.get_team(rt_id) is None:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)

    hub = get_stream_hub()

    async def _event_stream():
        # Initial marker so the client knows the connection is live
        yield f"event: connected\ndata: {_json.dumps({'rt_id': rt_id})}\n\n"

        keepalive_interval = 15.0
        sub = hub.subscribe(rt_id)
        clean_exit = False
        try:
            sub_iter = sub.__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(
                        sub_iter.__anext__(), timeout=keepalive_interval,
                    )
                except asyncio.TimeoutError:
                    # SSE comment line (ignored by EventSource) keeps the conn alive
                    yield f": keepalive {int(asyncio.get_running_loop().time())}\n\n"
                    continue
                except StopAsyncIteration:
                    clean_exit = True
                    break
                except asyncio.CancelledError:
                    break

                event_type = event.get("type", "message")
                payload = _json.dumps(event, ensure_ascii=False)
                yield f"event: {event_type}\ndata: {payload}\n\n"

            if clean_exit:
                # Signal the client to stop subscribing (roundtable closed).
                yield f"event: closed\ndata: {_json.dumps({'rt_id': rt_id})}\n\n"
        finally:
            # Deterministic cleanup: don't rely on GC finalization
            sub.close()

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/roundtables/{rt_id}/outline")
async def get_outline(request: Request, rt_id: int):
    """Get the secretary's most recent outline for this roundtable.

    Used by the frontend on page load / refresh to sync the outline panel
    independent of SSE event delivery."""
    core = request.app.state.core
    from .data_access import DataAccess
    from . import get_roundtable_manager

    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    if mgr.get_team(rt_id) is None:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)

    data = DataAccess(core)
    outline = data.get_latest_outline(rt_id)
    history = data.get_outline_history(rt_id)
    round_number = history[-1]["round_number"] if history else 0
    return JSONResponse({
        "rt_id": rt_id,
        "outline": outline or "",
        "round_number": round_number,
    })


@router.post("/api/roundtables/{rt_id}/ask")
async def ask_round(request: Request, rt_id: int, body: AskRoundRequest):
    """提问一轮"""
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    results = await team.start_round(question=body.question, mentioned=body.mentioned)
    for msg in results:
        msg["roundtable_id"] = rt_id
        data.insert_roundtable_message(**msg)
    data.update_roundtable(rt_id, round_count=team.round_number)

    await mgr.after_round(rt_id)

    return JSONResponse({"round_number": team.round_number, "messages": results})


@router.post("/api/roundtables/{rt_id}/remove/{seat_id}")
async def remove_seat(request: Request, rt_id: int, seat_id: str):
    """强制移除坐席"""
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Roundtable manager not initialized"}, status_code=500)
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    seat = team.seats.get(seat_id)
    if not seat:
        return JSONResponse({"error": f"Seat {seat_id} not found"}, status_code=404)
    seat.state = "exited"
    return JSONResponse({"seat_id": seat_id, "status": "exited"})


@router.post("/api/roundtables/{rt_id}/pause")
async def pause_roundtable(request: Request, rt_id: int):
    """暂停圆桌"""
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if mgr:
        mgr.pause_team(rt_id)
    return JSONResponse({"roundtable_id": rt_id, "status": "paused"})


@router.post("/api/roundtables/{rt_id}/close")
async def close_roundtable(request: Request, rt_id: int):
    """结束圆桌"""
    core = request.app.state.core
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    team = mgr.get_team(rt_id)
    # Capture spark_id BEFORE close_team removes the team from registry
    spark_id = team.spark_id if team else 0
    if team:
        try:
            await team.execute_graduation_cycle(roundtable_id=rt_id)
        except Exception:
            logger.warning("[ideator] graduation failed during close", exc_info=True)
        mgr.close_team(rt_id)

    # Auto-generate project brief from the secretary's outline (best-effort).
    # Skipped for direct-roundtable mode (spark_id=0) since project_briefs are
    # keyed by spark_id. Any failure is logged and swallowed — does not affect
    # the close response.
    brief_id = None
    if spark_id:
        try:
            from .data_access import DataAccess
            from .project_brief import ProjectBriefService
            data = DataAccess(core)
            outline = data.get_latest_outline(rt_id) or ""
            if outline:
                service = ProjectBriefService(core, data)
                brief_id = await service.generate(
                    spark_id, outline_markdown=outline,
                )
        except Exception:
            logger.warning(
                "[ideator] auto-generate brief failed for rt=%s", rt_id, exc_info=True,
            )

    return JSONResponse({
        "roundtable_id": rt_id,
        "status": "closed",
        "brief_id": brief_id,
    })


@router.post("/api/roundtables/{rt_id}/supplement")
async def supplement_context(request: Request, rt_id: int, body: SupplementRequest):
    """中途补充资料"""
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    rt = data.get_roundtable(rt_id)
    if not rt:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    msg_id = data.insert_roundtable_message(
        roundtable_id=rt_id,
        round_number=rt.get("round_count", 0) + 1,
        sender_type="system",
        sender_name="system",
        sender_role=None,
        message_type="supplement",
        content=body.content,
        word_count=len(body.content.split()),
        mentioned_by=json.dumps([body.seat_id]),
    )
    return JSONResponse({"message_id": msg_id, "status": "supplemented"})


# ── Agent Team 扩展 API ─────────────────────────────────────


@router.post("/api/roundtables/{rt_id}/graduate")
async def trigger_graduation(request: Request, rt_id: int):
    """手动触发毕业决策"""
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Manager not initialized"}, status_code=500)
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    try:
        decision = await team.execute_graduation_cycle(roundtable_id=rt_id)
        return JSONResponse(decision)
    except Exception:
        logger.warning("[ideator] graduation failed", exc_info=True)
        return JSONResponse({"error": "Graduation failed"}, status_code=500)


@router.get("/api/roundtables/{rt_id}/memory")
async def get_team_memory(request: Request, rt_id: int, memory_type: str | None = None):
    """获取团队记忆"""
    core = request.app.state.core
    from .data_access import DataAccess
    data = DataAccess(core)
    rt = data.get_roundtable(rt_id)
    if not rt:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    memories = data.get_team_memory(spark_id=rt["spark_id"], memory_type=memory_type)
    return JSONResponse(memories)


@router.get("/api/roundtables/{rt_id}/watermark")
async def get_watermark(request: Request, rt_id: int):
    """获取上下文水位报告"""
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Manager not initialized"}, status_code=500)
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    if team._graduation:
        return JSONResponse({"report": team._graduation.report()})
    return JSONResponse({"error": "Watermark not available"}, status_code=500)
