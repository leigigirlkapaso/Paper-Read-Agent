"""VoiceEngine chunked STT + audio concatenation tests."""
import pytest
import tempfile
from pathlib import Path


@pytest.fixture
def temp_audio_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


class TestVoiceChunked:

    def test_transcribe_chunk_method_exists(self):
        """transcribe_chunk should be available on VoiceEngine."""
        from paperreadagent.core import create_core
        from paperreadagent.modules.thinker.voice import VoiceEngine

        core = create_core(config_path="config.yaml", db_path=":memory:")
        engine = VoiceEngine(core)
        assert hasattr(engine, "transcribe_chunk")
        assert callable(engine.transcribe_chunk)

    def test_save_full_audio_writes_file(self, temp_audio_dir):
        """save_full_audio should concatenate chunks into a file."""
        from paperreadagent.core import create_core
        from paperreadagent.modules.thinker.voice import VoiceEngine

        core = create_core(config_path="config.yaml", db_path=":memory:")
        engine = VoiceEngine(core)
        chunks = [b"\x00" * 100, b"\x01" * 100, b"\x02" * 100]

        path = engine.save_full_audio(123, chunks, str(temp_audio_dir))
        assert Path(path).exists()
        assert Path(path).stat().st_size == 300
        assert "rehearsal_123_full.opus" in path

    @pytest.mark.asyncio
    async def test_set_audio_path_records_in_db(self):
        """set_audio_path should record the full audio path in the DB."""
        from paperreadagent.core import create_core
        from paperreadagent.modules.thinker import register
        from paperreadagent.modules.thinker.rehearsal import RehearsalEngine

        core = create_core(config_path="config.yaml", db_path=":memory:")
        register(core)
        engine = RehearsalEngine(core)

        rid = await engine.create(title="T", question_list_source="", question_list_content="")
        test_path = "projects/test/sessions/001/full_audio.opus"
        await engine.set_audio_path(rid, test_path)

        row = engine._core.db.conn.execute(
            "SELECT full_audio_path FROM thinker_rehearsals WHERE id = ?", (rid,)
        ).fetchone()
        assert row["full_audio_path"] == test_path
