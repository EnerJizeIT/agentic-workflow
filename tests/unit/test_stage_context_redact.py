"""T46 (AC27, R2, arch §9): one secret rule for the stage text.

The block stays, the value goes: ``token=…`` becomes ``<redacted: token>``,
a secret file's patch is dropped as a whole while its name survives.
"""
from __future__ import annotations

from awf.redact import FILE_MARKER, is_secret_file, is_secret_name, redact

SECRET_VALUE = "abc123-SUPER-SECRET"


def test_T46_named_value_is_replaced_by_its_marker() -> None:
    text = f"curl -H 'x: 1'\nDB token={SECRET_VALUE} done\n"
    masked, count = redact(text)
    assert SECRET_VALUE not in masked
    assert "<redacted: token>" in masked
    assert count == 1


def test_T46_secret_file_patch_is_dropped_and_named() -> None:
    diff = (
        "diff --git a/awf/x.py b/awf/x.py\n"
        "--- a/awf/x.py\n"
        "+++ b/awf/x.py\n"
        "+print('ok')\n"
        "diff --git a/.env b/.env\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        f"+API_TOKEN={SECRET_VALUE}\n"
    )
    masked, count = redact(diff)
    assert SECRET_VALUE not in masked
    assert FILE_MARKER in masked
    assert ".env" in masked
    assert "+print('ok')" in masked
    assert count == 1


def test_T46_stat_line_of_a_secret_file_keeps_the_name() -> None:
    masked, count = redact(" config.yaml | 4 ++--\n .env | 2 ++\n")
    assert SECRET_VALUE not in masked
    assert FILE_MARKER in masked
    assert ".env" in masked
    assert count == 1


def test_T46_already_masked_text_is_not_counted_twice() -> None:
    masked, count = redact("password: <redacted: password>")
    assert masked == "password: <redacted: password>"
    assert count == 0


def test_secret_name_and_file_vocabulary() -> None:
    assert is_secret_name("API_TOKEN")
    assert is_secret_name("db_password")
    assert not is_secret_name("pipeline")
    assert is_secret_file(".env")
    assert is_secret_file("config/prod.env.local")
    assert is_secret_file("certs/server.pem")
    assert is_secret_file("keys/id_rsa")
    assert not is_secret_file("awf/stage_context.py")


def test_redact_keeps_unrelated_text_bytes() -> None:
    text = "## Стадия\n- todo: TODO-0379\n"
    assert redact(text) == (text, 0)
