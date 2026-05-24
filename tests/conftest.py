from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aegis.api import create_app
from aegis.storage import reset_db


@pytest.fixture
def app(tmp_path):
    db_url = f"sqlite:///{tmp_path}/test.db"
    reset_db(db_url)
    return create_app(db_url=db_url)


@pytest.fixture
def client(app):
    return TestClient(app)
