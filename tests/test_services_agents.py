"""Tests for agp.services.agents."""

from __future__ import annotations

from _base import AgpTestCase

from agp.db import SessionLocal
from agp.enums import AgentStatus
from agp.models import Agent, utc_now
from agp.services.agents import agent_down_service
from agp.services.exceptions import BadRequestError


def _seed_idle_agent(session, agent_id: str = "agt_down") -> Agent:
    now = utc_now()
    ag = Agent(
        agent_id=agent_id,
        capabilities=["python"],
        metadata_json={},
        queue_id=f"agent:{agent_id}",
        status=AgentStatus.IDLE.value,
        last_heartbeat_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(ag)
    session.flush()
    return ag


class AgentDownModeValidationTest(AgpTestCase):
    def test_invalid_mode_raises_bad_request(self) -> None:
        session = SessionLocal()
        try:
            _seed_idle_agent(session, "agt_invalid_mode")
            session.commit()
            with self.assertRaises(BadRequestError) as ctx:
                agent_down_service(session, agent_id="agt_invalid_mode", mode="invalid")
            self.assertIn("invalid", str(ctx.exception))
        finally:
            session.close()

    def test_drain_mode_works(self) -> None:
        session = SessionLocal()
        try:
            _seed_idle_agent(session, "agt_drain")
            session.commit()
            result = agent_down_service(session, agent_id="agt_drain", mode="drain")
            self.assertEqual(result["status"], AgentStatus.DRAINING.value)
        finally:
            session.close()

    def test_force_mode_works(self) -> None:
        session = SessionLocal()
        try:
            _seed_idle_agent(session, "agt_force")
            session.commit()
            result = agent_down_service(session, agent_id="agt_force", mode="force")
            self.assertEqual(result["status"], "deleted")
            self.assertIsNone(session.get(Agent, "agt_force"))
        finally:
            session.close()
