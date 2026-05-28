from llm.llm_interface import output_guard_blocks_model_text


def test_output_guard_blocks_gateway_high_risk_rejection():
    assert output_guard_blocks_model_text(
        "The request was rejected because it was considered high risk"
    )

