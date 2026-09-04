from decimal import Decimal

import pytest

from screener.provenance import GIT_SHA_ENV, config_hash, git_sha, require_git_sha

SCORING_CONFIG = {"cutoff_offset": "1 day 6 hours", "min_peer_count": 20}


def test_key_order_does_not_change_the_hash():
    reordered = dict(reversed(list(SCORING_CONFIG.items())))
    assert config_hash(SCORING_CONFIG) == config_hash(reordered)


def test_changing_a_value_changes_the_hash():
    changed = {**SCORING_CONFIG, "min_peer_count": 21}
    assert config_hash(SCORING_CONFIG) != config_hash(changed)


def test_a_number_and_its_string_form_hash_differently():
    # Otherwise a config that started storing "20" instead of 20 would compare
    # equal to the old one and claim a comparability it does not have.
    assert config_hash({"n": 20}) != config_hash({"n": "20"})


def test_a_type_json_cannot_render_deterministically_is_refused():
    # Decimal's repr is not a stable cross-version contract, so accepting it
    # would produce a hash that changes on an interpreter upgrade.
    with pytest.raises(TypeError, match="cannot serialise Decimal"):
        config_hash({"threshold": Decimal("1.5")})


def test_the_hash_is_thirty_two_raw_bytes():
    # scoring_run.config_hash is bytea, so digest() rather than hexdigest().
    digest = config_hash(SCORING_CONFIG)
    assert isinstance(digest, bytes)
    assert len(digest) == 32


def test_the_build_sha_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(GIT_SHA_ENV, "0123456789abcdef")
    assert git_sha() == "0123456789abcdef"
    assert require_git_sha() == "0123456789abcdef"


def test_an_unknown_build_is_reported_by_git_sha_but_refused_by_require(monkeypatch):
    # /status must still answer from a hand-built container, but a scoring_run
    # row stamped "unknown" is a permanent lie in an append-only table.
    monkeypatch.delenv(GIT_SHA_ENV, raising=False)
    monkeypatch.setattr("screener.provenance.git._from_git", lambda: None)
    assert git_sha() == "unknown"
    with pytest.raises(RuntimeError, match=GIT_SHA_ENV):
        require_git_sha()


def test_a_config_hash_round_trips_through_the_bytea_column(fresh_db):
    # Demonstrates the bytes/bytea claim against a real database rather than
    # asserting it: a hexdigest string would be silently accepted by psycopg
    # and come back as something else entirely.
    digest = config_hash(SCORING_CONFIG)
    with fresh_db.cursor() as cur:
        cur.execute("insert into weight_version (code) values ('t') returning id")
        weight_version_id = cur.fetchone()[0]
        cur.execute(
            "insert into scoring_logic_version (description) values ('t') returning id"
        )
        logic_version_id = cur.fetchone()[0]
        cur.execute(
            """
            insert into scoring_run (
                as_of_range, cutoff_offset, logic_version_id, weight_version_id,
                status, emits_alerts, git_sha, config_hash, started_at, outcome
            ) values (
                daterange('2026-01-01', '2026-01-02'), interval '1 day 6 hours',
                %s, %s, 'experiment', false, 'abc123', %s, now(), 'running'
            ) returning id
            """,
            (logic_version_id, weight_version_id, digest),
        )
        run_id = cur.fetchone()[0]
        cur.execute("select config_hash from scoring_run where id = %s", (run_id,))
        stored = cur.fetchone()[0]

    assert bytes(stored) == digest
