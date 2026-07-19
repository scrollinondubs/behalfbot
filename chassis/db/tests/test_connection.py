#!/usr/bin/env python3
"""test_connection.py - DSN resolution and the loud-failure contract.

The property under test is not "it connects" - it is "it never quietly
degrades". A Pacman queue that returns zero rows because Postgres is
unreachable is indistinguishable from an empty queue, and the queue is the
only durable record that a URL was ever submitted. Every failure path here has
to raise, and the message has to say what to do about it.

Run:
    python3 -m pytest chassis/db/tests/test_connection.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from chassis.db.connection import (  # noqa: E402
    DSN_ENV_VARS,
    ChassisDBUnavailable,
    connect,
    get_dsn,
    is_configured,
)

DSN = "postgresql://u:p@postgres:5432/chassis"


class TestDsnResolution(unittest.TestCase):
    def test_prefers_chassis_pg_dsn(self):
        env = {"CHASSIS_PG_DSN": DSN, "BEHALFBOT_PG_DSN": "postgresql://wrong", "JAX_PG_DSN": "postgresql://wrong"}
        self.assertEqual(get_dsn(env), DSN)

    def test_falls_back_to_behalfbot_pg_dsn(self):
        """The name the dating plugin's selector reads first."""
        self.assertEqual(get_dsn({"BEHALFBOT_PG_DSN": DSN}), DSN)

    def test_falls_back_to_jax_pg_dsn(self):
        """Host-side legacy fallback, per the reference install's docs/jax-db.md."""
        self.assertEqual(get_dsn({"JAX_PG_DSN": DSN}), DSN)

    def test_resolution_order_matches_the_documented_constant(self):
        self.assertEqual(DSN_ENV_VARS, ("CHASSIS_PG_DSN", "BEHALFBOT_PG_DSN", "JAX_PG_DSN"))
        for index, var in enumerate(DSN_ENV_VARS):
            env = {v: f"postgresql://{v}" for v in DSN_ENV_VARS[index:]}
            self.assertEqual(get_dsn(env), f"postgresql://{var}")

    def test_whitespace_only_values_are_treated_as_unset(self):
        """A trailing-whitespace env var has bitten this stack before."""
        self.assertEqual(get_dsn({"CHASSIS_PG_DSN": "   ", "BEHALFBOT_PG_DSN": DSN}), DSN)

    def test_values_are_stripped(self):
        self.assertEqual(get_dsn({"CHASSIS_PG_DSN": f"  {DSN}  "}), DSN)


class TestUnconfiguredFailure(unittest.TestCase):
    def test_raises_rather_than_returning_none(self):
        with self.assertRaises(ChassisDBUnavailable):
            get_dsn({})

    def test_reason_is_machine_readable(self):
        with self.assertRaises(ChassisDBUnavailable) as ctx:
            get_dsn({})
        self.assertEqual(ctx.exception.reason, "unconfigured")

    def test_message_names_every_accepted_variable(self):
        """An operator reading this must not have to grep for the var name."""
        with self.assertRaises(ChassisDBUnavailable) as ctx:
            get_dsn({})
        for var in DSN_ENV_VARS:
            self.assertIn(var, str(ctx.exception))

    def test_message_gives_the_dsn_format(self):
        with self.assertRaises(ChassisDBUnavailable) as ctx:
            get_dsn({})
        self.assertIn("postgresql://", str(ctx.exception))


class TestIsConfigured(unittest.TestCase):
    def test_true_when_set(self):
        self.assertTrue(is_configured({"CHASSIS_PG_DSN": DSN}))

    def test_false_when_unset(self):
        self.assertFalse(is_configured({}))

    def test_does_not_raise(self):
        is_configured({})


class TestConnectFailure(unittest.TestCase):
    def test_unreachable_host_raises_chassis_db_unavailable(self):
        """psycopg's own exception is wrapped so callers catch one type."""
        with self.assertRaises(ChassisDBUnavailable) as ctx:
            connect(dsn="postgresql://u:p@127.0.0.1:1/definitely_not_here")
        self.assertEqual(ctx.exception.reason, "unreachable")

    def test_unreachable_message_tells_the_operator_where_to_look(self):
        with self.assertRaises(ChassisDBUnavailable) as ctx:
            connect(dsn="postgresql://u:p@127.0.0.1:1/definitely_not_here")
        message = str(ctx.exception)
        self.assertIn("docker compose ps postgres", message)
        self.assertIn("postgres:5432", message)


if __name__ == "__main__":
    unittest.main()
