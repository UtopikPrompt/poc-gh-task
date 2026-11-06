from fastapi.testclient import TestClient

from langgraph_service.model_artifact_api import create_app, list_model_filenames


def test_model_listing_includes_only_regular_gguf_files(tmp_path):
    (tmp_path / "z-model.gguf").touch()
    (tmp_path / "a-model.gguf").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "directory.gguf").mkdir()
    (tmp_path / "invalid name.gguf").touch()
    (tmp_path / "linked-model.gguf").symlink_to(tmp_path / "a-model.gguf")

    assert list_model_filenames(tmp_path) == ["a-model.gguf", "z-model.gguf"]


def test_available_model_endpoint_requires_service_token_and_lists_files(tmp_path):
    (tmp_path / "model.gguf").touch()
    client = TestClient(create_app(model_directory=tmp_path, service_token="secret"))

    unauthorized = client.get("/internal/models/available")
    wrong_token = client.get(
        "/internal/models/available",
        headers={"Authorization": "Bearer wrong"},
    )
    authorized = client.get(
        "/internal/models/available",
        headers={"Authorization": "Bearer secret"},
    )

    assert unauthorized.status_code == wrong_token.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {"models": ["model.gguf"]}


def test_available_model_endpoint_fails_closed_when_storage_is_unavailable(tmp_path):
    client = TestClient(
        create_app(model_directory=tmp_path / "missing", service_token="secret")
    )

    response = client.get(
        "/internal/models/available",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "model storage is unavailable"}
