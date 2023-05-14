import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
from transcribee_backend.auth import (
    change_user_password,
    create_user,
    generate_user_token,
)
from transcribee_backend.config import settings
from transcribee_backend.db import get_session
from transcribee_backend.exceptions import UserAlreadyExists
from transcribee_backend.main import app
from transcribee_backend.models import User


@pytest.fixture(autouse=True, scope="session")
def settings_with_tmpdir():
    with tempfile.TemporaryDirectory() as tmpfile:
        settings.storage_path = Path(tmpfile)
        yield settings


@pytest.fixture
def client(memory_session: Session):
    def get_session_override():
        return memory_session

    app.dependency_overrides[get_session] = get_session_override

    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def logged_in_client(memory_session: Session, auth_token):
    def get_session_override():
        return memory_session

    app.dependency_overrides[get_session] = get_session_override

    client = TestClient(app, headers={"Authorization": f"Token {auth_token}"})
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def alembic_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    return engine


@pytest.fixture(params=[False, True])
def memory_session(request, alembic_engine, alembic_runner):
    # We run all tests two times:
    # - The first time we setup the table directly via sqlmodel
    # - The second time we migrate using alembic
    # This allows quick development (no need to generate migrations),
    # but also ensures that all the migrations work in the end
    #
    # Note: This is probably redundant, because we also check that the
    # migrations generate the proper models in
    # `test_model_definitions_match_ddl`
    migrate = request.param
    if migrate:
        for head in alembic_runner.heads:
            alembic_runner.migrate_up_to(head)
    else:
        SQLModel.metadata.create_all(alembic_engine)
    with Session(alembic_engine) as session:
        yield session


@pytest.fixture
def user(memory_session: Session):
    username = "test_user"
    password = "test_user_pass"
    try:
        user = create_user(session=memory_session, username=username, password=password)
    except UserAlreadyExists:
        user = change_user_password(
            session=memory_session, username=username, new_password=password
        )

    return user


@pytest.fixture
def auth_token(user: User, memory_session: Session):
    user_token, db_token = generate_user_token(user)
    memory_session.add(db_token)
    memory_session.commit()
    return user_token