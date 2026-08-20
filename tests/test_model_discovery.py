from hermes_cursor_login.native.model_discovery import parse_usable_models
from hermes_cursor_login.native.proto import agent_pb2


def test_model_catalog_normalizes_aliases_deduplicates_and_sorts() -> None:
    payload = agent_pb2.GetUsableModelsResponse(
        models=[
            agent_pb2.ModelDetails(model_id="gpt-5.4-medium"),
            agent_pb2.ModelDetails(model_id="cursor-composer-2.5"),
            agent_pb2.ModelDetails(model_id="composer-2.5"),
            agent_pb2.ModelDetails(model_id="auto"),
            agent_pb2.ModelDetails(model_id=""),
        ]
    ).SerializeToString()

    assert parse_usable_models(payload) == ["composer-2.5", "default", "gpt-5.4-medium"]
