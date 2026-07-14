"""Tests for atomic recorder history replacement."""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from homeassistant.components.recorder.db_schema import (
    SCHEMA_VERSION,
    Base,
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.table_managers.states_meta import (
    StatesMetaManager,
)
from homeassistant.components.recorder.table_managers.statistics_meta import (
    StatisticsMetaManager,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from custom_components.solaredge_history_downloader.history import HistoryPoint
from custom_components.solaredge_history_downloader.recorder import (
    ReplaceHistoryTask,
    _statistics_metadata,
)

ENTITY_ID = "sensor.solar_production_monthly"


class FakeRecorder:
    """Recorder surface required by ReplaceHistoryTask._replace."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.thread_id = threading.get_ident()
        self.schema_version = SCHEMA_VERSION
        self.max_bind_vars = 999
        self._session_factory = session_factory
        self.states_meta_manager = StatesMetaManager(self)  # type: ignore[arg-type]
        self.statistics_meta_manager = StatisticsMetaManager(self)  # type: ignore[arg-type]

    def get_session(self) -> Session:
        """Return a database session."""
        return self._session_factory()


@pytest.fixture
def recorder_database() -> tuple[FakeRecorder, sessionmaker[Session]]:
    """Create a recorder schema containing history that must be replaced."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory.begin() as session:
        states_metadata = StatesMeta(entity_id=ENTITY_ID)
        statistics_metadata = StatisticsMeta.from_meta(
            _statistics_metadata(ENTITY_ID, "Solar production monthly", "kWh")
        )
        session.add_all([states_metadata, statistics_metadata])
        session.flush()

        first_state = States(
            metadata_id=states_metadata.metadata_id,
            state="1",
            last_updated_ts=1,
        )
        session.add(first_state)
        session.flush()
        session.add(
            States(
                metadata_id=states_metadata.metadata_id,
                state="2",
                last_updated_ts=2,
                old_state_id=first_state.state_id,
            )
        )
        session.add_all(
            [
                Statistics(
                    metadata_id=statistics_metadata.id,
                    created_ts=1,
                    start_ts=1,
                    state=2,
                    sum=2,
                ),
                StatisticsShortTerm(
                    metadata_id=statistics_metadata.id,
                    created_ts=1,
                    start_ts=1,
                    state=2,
                    sum=2,
                ),
            ]
        )

    yield FakeRecorder(session_factory), session_factory
    engine.dispose()


def test_replace_history_commits_states_and_long_term_statistics(
    recorder_database: tuple[FakeRecorder, sessionmaker[Session]],
) -> None:
    recorder, session_factory = recorder_database
    task = _task(_points())

    result, _, _ = task._replace(recorder)  # noqa: SLF001

    assert result.deleted_states == 2
    assert result.deleted_short_term_statistics == 1
    assert result.deleted_long_term_statistics == 1
    assert result.imported_states == 2
    assert result.imported_long_term_statistics == 2
    with session_factory() as session:
        states = session.query(States).order_by(States.last_updated_ts).all()
        statistics = session.query(Statistics).order_by(Statistics.start_ts).all()
        assert [state.state for state in states] == ["1", "3"]
        assert all(state.old_state_id is None for state in states)
        assert len({state.attributes_id for state in states}) == 1
        assert [statistic.state for statistic in statistics] == [1, 3]
        assert [statistic.sum for statistic in statistics] == [1, 3]
        assert session.query(StatisticsShortTerm).count() == 0


def test_replace_history_rolls_back_all_deletes_on_invalid_statistics(
    recorder_database: tuple[FakeRecorder, sessionmaker[Session]],
) -> None:
    recorder, session_factory = recorder_database
    duplicate_hour = HistoryPoint(
        start=datetime(2024, 1, 1, 0, 30, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, tzinfo=UTC),
        interval_value=Decimal("2"),
        state=Decimal("3"),
        sum=Decimal("3"),
    )

    with pytest.raises(ValueError, match="duplicate"):
        _task([_points()[0], duplicate_hour])._replace(recorder)  # noqa: SLF001

    with session_factory() as session:
        assert [state.state for state in session.query(States).all()] == ["1", "2"]
        assert session.query(Statistics).count() == 1
        assert session.query(StatisticsShortTerm).count() == 1


def _points() -> list[HistoryPoint]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        HistoryPoint(
            start=start,
            end=start + timedelta(hours=1),
            interval_value=Decimal("1"),
            state=Decimal("1"),
            sum=Decimal("1"),
        ),
        HistoryPoint(
            start=start + timedelta(hours=1),
            end=start + timedelta(hours=2),
            interval_value=Decimal("2"),
            state=Decimal("3"),
            sum=Decimal("3"),
        ),
    ]


def _task(points: list[HistoryPoint]) -> ReplaceHistoryTask:
    return ReplaceHistoryTask(
        entity_id=ENTITY_ID,
        name="Solar production monthly",
        unit="kWh",
        attributes={"unit_of_measurement": "kWh"},
        points=tuple(points),
        on_done=lambda *_: None,
    )
