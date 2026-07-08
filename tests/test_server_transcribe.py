import pytest

from server.queue import TranscriptionJob
from server.transcribe import _validate_vibevoice_output


def test_vibevoice_invalid_json_marks_job_failed() -> None:
    job = TranscriptionJob(audio_duration_seconds=12.5, audio_mime="audio/wav")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        _validate_vibevoice_output("not json", job)

    assert job.error_message is not None
    assert "not valid JSON" in job.error_message


def test_vibevoice_valid_segments_leave_job_error_empty() -> None:
    job = TranscriptionJob(audio_duration_seconds=12.5, audio_mime="audio/wav")

    _validate_vibevoice_output('[{"Start": 0, "End": 1, "Content": "hello"}]', job)

    assert job.error_message is None
