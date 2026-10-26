import ssl

import pytest

from ops_sentinel import db


def test_certificate_validation_is_on_by_default() -> None:
    assert db._ssl_options() == {"cert_reqs": ssl.CERT_REQUIRED}


def test_certificate_validation_can_be_disabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db.settings, "exasol_validate_certificate", False)

    assert db._ssl_options() == {"cert_reqs": ssl.CERT_NONE}


def test_a_pool_without_a_user_names_the_setting_to_fix_and_leaks_no_slot() -> None:
    pool = db.ExasolConnectionPool(
        max_size=1, credentials=db.ExasolCredentials(user="", password="", setting_name="EXASOL_EXECUTOR_USER")
    )

    with pytest.raises(db.DatabaseConfigurationError, match="EXASOL_EXECUTOR_USER"):
        with pool.connection():
            pass

    assert pool._created == 0
