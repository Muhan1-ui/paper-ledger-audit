"""Relational read-model projection for the immutable paper ledger."""

from decimal import Decimal

from sqlalchemy import JSON, Column, Index, String, create_engine, func, select, text
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from .service_config import database_url_from_environment


Base = declarative_base()


class MatchProjection(Base):
    """One queryable row per frozen prediction, keyed by immutable ledger hash."""

    __tablename__ = "match_projection"

    prediction_hash = Column(String(64), primary_key=True)
    match_id = Column(String(32), nullable=False)
    kickoff_at = Column(String(40), nullable=False)
    captured_at = Column(String(40), nullable=False)
    prediction_role = Column(String(16), nullable=False)
    competition = Column(String(255), nullable=True)
    home_team = Column(String(255), nullable=True)
    away_team = Column(String(255), nullable=True)
    official_match_url = Column(String(2048), nullable=False)
    outcomes = Column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_match_projection_match_id", "match_id"),
        Index("ix_match_projection_captured_at", "captured_at"),
    )


class SettlementProjection(Base):
    """Official-result facts linked to a frozen prediction hash."""

    __tablename__ = "settlement_projection"

    settlement_hash = Column(String(64), primary_key=True)
    prediction_hash = Column(String(64), nullable=False, index=True)
    actual_outcome = Column(String(16), nullable=False)
    official_result_url = Column(String(2048), nullable=False)
    final_score = Column(JSON, nullable=True)


class SettlementInvalidationProjection(Base):
    """Append-only corrections that exclude an earlier settlement from fact views."""

    __tablename__ = "settlement_invalidation_projection"

    invalidation_hash = Column(String(64), primary_key=True)
    settlement_hash = Column(String(64), nullable=False, unique=True)
    prediction_hash = Column(String(64), nullable=False, index=True)
    reason = Column(String(1024), nullable=False)


class EvidenceEventProjection(Base):
    """Queryable copy of an immutable source observation or collection failure."""

    __tablename__ = "evidence_event_projection"

    record_hash = Column(String(64), primary_key=True)
    match_id = Column(String(32), nullable=False, index=True)
    record_type = Column(String(32), nullable=False)
    evidence_kind = Column(String(64), nullable=False)
    collector = Column(String(255), nullable=False)
    parser_version = Column(String(128), nullable=False)
    event_at = Column(String(40), nullable=False)
    source_url = Column(String(2048), nullable=True)
    published_at = Column(String(40), nullable=True)
    published_at_unavailable_reason = Column(String(1024), nullable=True)
    observed_facts = Column(JSON, nullable=True)
    reason_code = Column(String(64), nullable=True)
    reason = Column(String(1024), nullable=True)

    __table_args__ = (
        Index("ix_evidence_event_projection_match_event", "match_id", "event_at"),
    )


def _json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _engine_options(database_url):
    if database_url.startswith("sqlite:"):
        return {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    return {"pool_pre_ping": True}


class LedgerProjectionStore:
    """Projects append-only ledger facts into a database-owned read model."""

    def __init__(self, database_url):
        self.engine = create_engine(database_url, **_engine_options(database_url))
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def health_check(self):
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def sync_records(self, records):
        predictions = [record for record in records if record.get("record_type") == "prediction"]
        settlements = [record for record in records if record.get("record_type") == "settlement"]
        invalidations = [
            record for record in records
            if record.get("record_type") == "settlement_invalidation"
        ]
        evidence_events = [
            record for record in records
            if record.get("record_type") in {"source_observation", "collection_failure"}
        ]
        with self.session_factory() as session:
            for prediction in predictions:
                context = prediction.get("match_context", {})
                session.merge(MatchProjection(
                    prediction_hash=prediction["content_hash"],
                    match_id=prediction["match_id"],
                    kickoff_at=prediction["kickoff_at"],
                    captured_at=prediction["captured_at"],
                    prediction_role=prediction.get("prediction_role", "primary"),
                    competition=context.get("competition"),
                    home_team=context.get("home_team"),
                    away_team=context.get("away_team"),
                    official_match_url=prediction["source_urls"]["S1"],
                    outcomes=_json_value(prediction["outcomes"]),
                ))
            for settlement in settlements:
                session.merge(SettlementProjection(
                    settlement_hash=settlement["content_hash"],
                    prediction_hash=settlement["prediction_hash"],
                    actual_outcome=settlement["actual_outcome"],
                    official_result_url=settlement["official_result_url"],
                    final_score=_json_value(settlement.get("final_score")),
                ))
            for invalidation in invalidations:
                session.merge(SettlementInvalidationProjection(
                    invalidation_hash=invalidation["content_hash"],
                    settlement_hash=invalidation["settlement_hash"],
                    prediction_hash=invalidation["prediction_hash"],
                    reason=invalidation["reason"],
                ))
            for event in evidence_events:
                is_observation = event["record_type"] == "source_observation"
                session.merge(EvidenceEventProjection(
                    record_hash=event["content_hash"],
                    match_id=event["match_id"],
                    record_type=event["record_type"],
                    evidence_kind=event["evidence_kind"],
                    collector=event["collector"],
                    parser_version=event["parser_version"],
                    event_at=event["captured_at"] if is_observation else event["attempted_at"],
                    source_url=event.get("source_url") if is_observation else event.get("attempted_source_url"),
                    published_at=event.get("published_at") if is_observation else None,
                    published_at_unavailable_reason=(
                        event.get("published_at_unavailable_reason") if is_observation else None
                    ),
                    observed_facts=_json_value(event.get("observed_facts")) if is_observation else None,
                    reason_code=event.get("reason_code") if not is_observation else None,
                    reason=event.get("reason") if not is_observation else None,
                ))
            session.commit()
        return {
            "projected_predictions": len(predictions),
            "projected_settlements": len(settlements),
            "projected_invalidations": len(invalidations),
            "projected_evidence_events": len(evidence_events),
        }

    def list_matches(self, limit=20):
        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(MatchProjection))
            rows = session.scalars(
                select(MatchProjection)
                .order_by(MatchProjection.captured_at.desc(), MatchProjection.prediction_hash)
                .limit(limit)
            ).all()
        return total, [
            {
                "prediction_hash": row.prediction_hash,
                "match_id": row.match_id,
                "kickoff_at": row.kickoff_at,
                "captured_at": row.captured_at,
                "prediction_role": row.prediction_role,
                "competition": row.competition,
                "home_team": row.home_team,
                "away_team": row.away_team,
                "official_match_url": row.official_match_url,
                "outcomes": row.outcomes,
            }
            for row in rows
        ]

    def match_detail(self, match_id):
        with self.session_factory() as session:
            predictions = session.scalars(
                select(MatchProjection)
                .where(MatchProjection.match_id == match_id)
                .order_by(MatchProjection.captured_at, MatchProjection.prediction_hash)
            ).all()
            settlements = {
                row.prediction_hash: row
                for row in session.scalars(
                    select(SettlementProjection).where(
                        SettlementProjection.prediction_hash.in_(
                            [prediction.prediction_hash for prediction in predictions]
                        )
                    )
                ).all()
            } if predictions else {}
            invalidated_hashes = set(session.scalars(
                select(SettlementInvalidationProjection.settlement_hash).where(
                    SettlementInvalidationProjection.prediction_hash.in_(
                        [prediction.prediction_hash for prediction in predictions]
                    )
                )
            ).all()) if predictions else set()
            evidence_events = session.scalars(
                select(EvidenceEventProjection)
                .where(EvidenceEventProjection.match_id == match_id)
                .order_by(EvidenceEventProjection.event_at, EvidenceEventProjection.record_hash)
            ).all()
        if not predictions:
            return None
        items = []
        for prediction in predictions:
            settlement = settlements.get(prediction.prediction_hash)
            item = {
                "prediction_hash": prediction.prediction_hash,
                "captured_at": prediction.captured_at,
                "prediction_role": prediction.prediction_role,
                "outcomes": prediction.outcomes,
                "official_match_url": prediction.official_match_url,
                "settlement": None,
            }
            if settlement is not None and settlement.settlement_hash not in invalidated_hashes:
                item["settlement"] = {
                    "settlement_hash": settlement.settlement_hash,
                    "actual_outcome": settlement.actual_outcome,
                    "official_result_url": settlement.official_result_url,
                    "final_score": settlement.final_score,
                }
            items.append(item)
        first = predictions[0]
        return {
            "match_id": first.match_id,
            "competition": first.competition,
            "home_team": first.home_team,
            "away_team": first.away_team,
            "kickoff_at": first.kickoff_at,
            "total_predictions": len(items),
            "predictions": items,
            "evidence_events": [
                {
                    "record_hash": event.record_hash,
                    "record_type": event.record_type,
                    "evidence_kind": event.evidence_kind,
                    "collector": event.collector,
                    "parser_version": event.parser_version,
                    "event_at": event.event_at,
                    "source_url": event.source_url,
                    "published_at": event.published_at,
                    "published_at_unavailable_reason": event.published_at_unavailable_reason,
                    "observed_facts": event.observed_facts,
                    "reason_code": event.reason_code,
                    "reason": event.reason,
                }
                for event in evidence_events
            ],
        }


def create_store(database_url):
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("数据库连接地址不能为空")
    return LedgerProjectionStore(database_url.strip())


def create_configured_store():
    """Create the relational read model from private project configuration."""
    return create_store(database_url_from_environment())
