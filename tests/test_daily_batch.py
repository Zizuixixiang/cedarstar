from memory.daily_batch import DailyBatchProcessor


def test_default_events_always_has_at_least_one():
    """保证 Step 4 默认事件至少产出 1 条，否则 ChromaDB 会写空数据。"""
    processor = DailyBatchProcessor()
    result = processor._normalize_step4_events(
        raw_events=[],
        daily_summary_text="全天平淡，主要在日常互动中度过。",
        fallback_score=5,
        fallback_arousal=0.1,
        event_split_max=8,
    )

    assert len(result) >= 1
    assert result[0]["summary"]
