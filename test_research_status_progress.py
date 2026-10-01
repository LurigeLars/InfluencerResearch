import json

from research_status_summary import summarize_status


def test_summarize_status_exposes_live_progress(tmp_path):
    status_path = tmp_path / "creator_recent_check_status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "RUNNING",
                "started_at": "2026-10-01T12:57:24+00:00",
                "updated_at": "2026-10-01T12:58:10+00:00",
                "scope": "MONITORED",
                "progress": {
                    "stage": "DISCOVERY",
                    "phase": "SOURCE_COMPLETE",
                    "completed_sources": 3,
                    "total_sources": 7,
                    "current_creator": "nicholascrown",
                    "current_platform": "TIKTOK",
                    "discovered_items": 4,
                },
                "private_internal_field": "must-not-leak",
            }
        ),
        encoding="utf-8",
    )

    result = summarize_status(status_path)

    assert result is not None
    assert result["updated_at"] == "2026-10-01T12:58:10+00:00"
    assert result["progress"]["stage"] == "DISCOVERY"
    assert result["progress"]["completed_sources"] == 3
    assert result["progress"]["current_creator"] == "nicholascrown"
    assert "private_internal_field" not in result
