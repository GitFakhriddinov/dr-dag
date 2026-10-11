from dagdoctor.redact import contains_secret, redact_code, redact_log


def test_uri_password_is_removed_in_logs_and_code():
    text = 'psycopg2.connect("postgresql://etl_user:Very$ecret1@crm-db:5432/crm")'
    for redacted in (redact_log(text), redact_code(text)):
        assert "Very$ecret1" not in redacted
        assert "postgresql://etl_user:***@crm-db:5432/crm" in redacted


def test_log_mode_blanks_key_value_pairs_and_pii():
    text = "login ok password=hunter22 user=ali@example.com phone=+998901234567"
    redacted = redact_log(text)
    assert "hunter22" not in redacted
    assert "ali@example.com" not in redacted
    assert "+998901234567" not in redacted


def test_code_mode_keeps_code_patchable():
    code = "client = connect(host=conn.host, password=conn.password)"
    assert redact_code(code) == code


def test_code_mode_blanks_literal_secrets():
    code = 'API_TOKEN = "tok-123456"\nheaders = {"api_key": "abcd1234"}'
    redacted = redact_code(code)
    assert "tok-123456" not in redacted
    assert "abcd1234" not in redacted


def test_known_token_formats():
    text = "key AKIAABCDEFGHIJKLMNOP and ghp_" + "a" * 36 + " and Bearer abc.def.ghi123"
    redacted = redact_log(text)
    assert "AKIA" not in redacted
    assert "ghp_" not in redacted
    assert "abc.def.ghi123" not in redacted


def test_contains_secret():
    assert contains_secret('password = "letmein!"')
    assert not contains_secret("password = os.environ['DB_PASSWORD']")
