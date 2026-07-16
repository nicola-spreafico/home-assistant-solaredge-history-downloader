"""Atomic Home Assistant recorder history replacement."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    StateAttributes,
    States,
    StatesMeta,
    Statistics,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    get_short_term_statistics_run_cache,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import JSON_DUMP
from sqlalchemy.orm import Session

from .history import HistoryPoint, StatRow

if TYPE_CHECKING:
    from homeassistant.components.recorder.core import Recorder


_LOGGER = logging.getLogger(__name__)

CompletionCallback = Callable[
    ["ReplacementResult | None", "BaseException | None"], None
]


@dataclass(frozen=True, slots=True)
class ReplacementResult:
    """Counts from an atomic recorder history replacement."""

    deleted_states: int
    deleted_short_term_statistics: int
    deleted_long_term_statistics: int
    imported_states: int
    imported_long_term_statistics: int
    imported_short_term_statistics: int


@dataclass(slots=True)
class ReplaceHistoryTask(RecorderTask):
    """Replace one entity's raw state and statistics rows in one transaction."""

    entity_id: str
    name: str
    unit: str
    attributes: Mapping[str, Any]
    points: tuple[HistoryPoint, ...]
    stat_rows: tuple[StatRow, ...]
    last_reset: datetime | None
    continue_live_statistics: bool
    on_done: CompletionCallback

    def run(self, instance: Recorder) -> None:
        """Run in the serialized recorder thread."""
        try:
            result, statistic_metadata_id, created_states_metadata = self._replace(
                instance
            )
            instance.states_manager.evict_purged_entity_ids({self.entity_id})
            instance.statistics_meta_manager.reset()
            if created_states_metadata:
                instance.states_meta_manager.reset()
            instance.state_attributes_manager.reset()
            _evict_short_term_statistics_cache(instance.hass, statistic_metadata_id)
            with session_scope(
                session=instance.get_session(), read_only=True
            ) as session:
                instance.states_manager.load_from_db(session)
        except BaseException as err:
            instance.statistics_meta_manager.reset()
            instance.hass.loop.call_soon_threadsafe(self.on_done, None, err)
            raise
        instance.hass.loop.call_soon_threadsafe(self.on_done, result, None)

    def _replace(self, instance: Recorder) -> tuple[ReplacementResult, int, bool]:
        created_states_metadata = False
        with session_scope(session=instance.get_session()) as session:
            states_metadata_id = instance.states_meta_manager.get(
                self.entity_id, session, True
            )
            if states_metadata_id is None:
                states_metadata = StatesMeta(entity_id=self.entity_id)
                session.add(states_metadata)
                session.flush()
                states_metadata_id = states_metadata.metadata_id
                created_states_metadata = True

            attributes_id = (
                _get_or_create_attributes_id(session, self.attributes)
                if self.points
                else None
            )
            deleted_states = (
                session.query(States)
                .filter(States.metadata_id == states_metadata_id)
                .count()
            )
            (
                session.query(States)
                .filter(States.metadata_id == states_metadata_id)
                .update({States.old_state_id: None}, synchronize_session=False)
            )
            (
                session.query(States)
                .filter(States.metadata_id == states_metadata_id)
                .delete(synchronize_session=False)
            )

            statistic_metadata = _statistics_metadata(
                self.entity_id, self.name, self.unit
            )
            current_metadata = instance.statistics_meta_manager.get_many(
                session, statistic_ids={self.entity_id}
            )
            if (
                existing := current_metadata.get(self.entity_id)
            ) is not None and existing[1]["source"] != "recorder":
                raise ValueError(
                    f"Statistic {self.entity_id} is not owned by the recorder"
                )
            _, statistic_metadata_id = instance.statistics_meta_manager.update_or_add(
                session, statistic_metadata, current_metadata
            )

            deleted_short_term_statistics = (
                session.query(StatisticsShortTerm)
                .filter(StatisticsShortTerm.metadata_id == statistic_metadata_id)
                .count()
            )
            deleted_long_term_statistics = (
                session.query(Statistics)
                .filter(Statistics.metadata_id == statistic_metadata_id)
                .count()
            )
            (
                session.query(StatisticsShortTerm)
                .filter(StatisticsShortTerm.metadata_id == statistic_metadata_id)
                .delete(synchronize_session=False)
            )
            (
                session.query(Statistics)
                .filter(Statistics.metadata_id == statistic_metadata_id)
                .delete(synchronize_session=False)
            )

            now_timestamp = time.time()
            # When the entity is live-compiled, the recorder summarizes the
            # in-progress hour into a long-term row once the hour completes;
            # an imported long-term row at that hour would collide with the
            # summary. Rows at or after the current hour are dropped here and
            # rebuilt live: their energy is still carried by the short-term
            # seed row's sum below.
            stat_rows = self.stat_rows
            if self.continue_live_statistics:
                current_hour_ts = int(now_timestamp // 3600) * 3600
                stat_rows = tuple(
                    row for row in stat_rows if row.start.timestamp() < current_hour_ts
                )
            raw_states = [
                States(
                    metadata_id=states_metadata_id,
                    attributes_id=attributes_id,
                    state=_decimal_state(point.state),
                    last_updated_ts=(point.end - timedelta(microseconds=1)).timestamp(),
                    last_changed_ts=None,
                    last_reported_ts=None,
                    origin_idx=0,
                )
                for point in self.points
            ]
            statistics = [
                Statistics.from_stats(
                    statistic_metadata_id,
                    StatisticData(
                        start=row.start,
                        state=float(row.state),
                        sum=float(row.sum),
                    ),
                    now_timestamp,
                )
                for row in stat_rows
            ]
            _validate_unique_statistics_starts(statistics)
            seed_rows = self._short_term_seed_rows(statistic_metadata_id, now_timestamp)
            session.add_all(raw_states)
            session.add_all(statistics)
            session.add_all(seed_rows)
            session.flush()

        return (
            ReplacementResult(
                deleted_states=deleted_states,
                deleted_short_term_statistics=deleted_short_term_statistics,
                deleted_long_term_statistics=deleted_long_term_statistics,
                imported_states=len(raw_states),
                imported_long_term_statistics=len(statistics),
                imported_short_term_statistics=len(seed_rows),
            ),
            statistic_metadata_id,
            created_states_metadata,
        )

    def _short_term_seed_rows(
        self, statistic_metadata_id: int, now_timestamp: float
    ) -> list[StatisticsShortTerm]:
        """Build the short-term row that carries the imported sum forward.

        The sensor statistics compiler continues a sensor's running sum from
        the latest short-term row only; with every short-term row deleted the
        sum would restart near zero on the next 5-minute compilation, making
        every period spanning the replacement hugely negative. The seed also
        carries the target's current ``last_reset`` so that compilation does
        not misread the replacement as a meter reset.
        """
        if not self.continue_live_statistics or not self.stat_rows:
            return []
        # Two 5-minute slots back: past slots are never compiled again, so
        # the imminent compilation cannot collide with the seed row.
        seed_start_ts = int(now_timestamp // 300) * 300 - 600
        last_row = self.stat_rows[-1]
        return [
            StatisticsShortTerm.from_stats(
                statistic_metadata_id,
                StatisticData(
                    start=datetime.fromtimestamp(seed_start_ts, UTC),
                    state=float(last_row.state),
                    sum=float(last_row.sum),
                    last_reset=self.last_reset,
                ),
                now_timestamp,
            )
        ]


async def async_replace_history(
    hass: HomeAssistant,
    *,
    entity_id: str,
    name: str,
    unit: str,
    attributes: Mapping[str, Any],
    points: list[HistoryPoint],
    stat_rows: list[StatRow],
    last_reset: datetime | None,
    continue_live_statistics: bool,
) -> ReplacementResult:
    """Queue an atomic recorder replacement and await its completion."""
    future: asyncio.Future[ReplacementResult] = hass.loop.create_future()

    def on_done(result: ReplacementResult | None, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
            return
        assert result is not None
        future.set_result(result)

    get_instance(hass).queue_task(
        ReplaceHistoryTask(
            entity_id=entity_id,
            name=name,
            unit=unit,
            attributes=attributes,
            points=tuple(points),
            stat_rows=tuple(stat_rows),
            last_reset=last_reset,
            continue_live_statistics=continue_live_statistics,
            on_done=on_done,
        )
    )
    return await future


def _get_or_create_attributes_id(
    session: Session, attributes: Mapping[str, Any]
) -> int:
    shared_attributes = JSON_DUMP(dict(attributes))
    attributes_hash = StateAttributes.hash_shared_attrs_bytes(
        shared_attributes.encode()
    )
    existing = (
        session.query(StateAttributes.attributes_id)
        .filter(
            StateAttributes.hash == attributes_hash,
            StateAttributes.shared_attrs == shared_attributes,
        )
        .first()
    )
    if existing is not None:
        return int(existing[0])

    state_attributes = StateAttributes(
        hash=attributes_hash, shared_attrs=shared_attributes
    )
    session.add(state_attributes)
    session.flush()
    return state_attributes.attributes_id


def _statistics_metadata(entity_id: str, name: str, unit: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        unit_class="energy",
        name=name,
        source="recorder",
        statistic_id=entity_id,
        unit_of_measurement=unit,
    )


def _decimal_state(value: Any) -> str:
    return format(value, "f")


def _validate_unique_statistics_starts(statistics: list[Statistics]) -> None:
    starts = [statistic.start_ts for statistic in statistics]
    if len(starts) != len(set(starts)):
        raise ValueError(
            "Downloaded points collapse onto duplicate Home Assistant statistic hours"
        )


def _evict_short_term_statistics_cache(hass: HomeAssistant, metadata_id: int) -> None:
    run_cache = get_short_term_statistics_run_cache(hass)
    run_cache._latest_id_by_metadata_id.pop(metadata_id, None)  # noqa: SLF001
