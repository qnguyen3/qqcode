from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from vibe.core.aws import (
    AwsCredentials,
    load_aws_credentials,
    load_profile_credentials,
    region_from_endpoint,
    sign_request,
    signing_key,
)

# AWS "Signature Version 4 test suite" example: IAM ListUsers on 2015-08-30.
_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_CREDENTIALS = AwsCredentials("AKIDEXAMPLE", _SECRET)
_TIMESTAMP = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)


def test_signing_key_matches_the_aws_example() -> None:
    key = signing_key(_SECRET, "20150830", "us-east-1", "iam")

    assert (
        key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"
    )


def test_sign_request_matches_the_aws_example() -> None:
    headers = sign_request(
        method="GET",
        url="https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        body=b"",
        credentials=_CREDENTIALS,
        region="us-east-1",
        service="iam",
        extra_headers={
            "content-type": "application/x-www-form-urlencoded; charset=utf-8"
        },
        timestamp=_TIMESTAMP,
    )

    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
    assert "host" not in headers


def test_sign_request_includes_the_session_token() -> None:
    credentials = AwsCredentials("AKID", "SECRET", session_token="session")

    headers = sign_request(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse",
        body=b"{}",
        credentials=credentials,
        region="us-east-1",
        timestamp=_TIMESTAMP,
    )

    assert headers["x-amz-security-token"] == "session"
    assert "x-amz-security-token" in headers["Authorization"]


def test_region_from_endpoint_reads_standard_hosts() -> None:
    assert (
        region_from_endpoint(
            "https://bedrock-runtime.eu-central-1.amazonaws.com/model/x"
        )
        == "eu-central-1"
    )
    assert region_from_endpoint("https://example.test/model/x") is None


def test_load_profile_credentials_reads_a_named_profile(tmp_path: Path) -> None:
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[default]\n"
        "aws_access_key_id = DEFAULTKEY\n"
        "aws_secret_access_key = defaultsecret\n"
        "[work]\n"
        "aws_access_key_id = WORKKEY\n"
        "aws_secret_access_key = worksecret\n"
        "aws_session_token = worktoken\n",
        encoding="utf-8",
    )

    assert load_profile_credentials("missing", credentials_file) is None
    credentials = load_profile_credentials("work", credentials_file)

    assert credentials == AwsCredentials("WORKKEY", "worksecret", "worktoken")


def test_load_aws_credentials_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENVKEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "envsecret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "envtoken")

    resolved = load_aws_credentials()

    assert resolved is not None
    credentials, source = resolved
    assert credentials == AwsCredentials("ENVKEY", "envsecret", "envtoken")
    assert source == "AWS access keys"


def test_load_aws_credentials_reads_the_shared_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[default]\naws_access_key_id = FILEKEY\naws_secret_access_key = filesecret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    resolved = load_aws_credentials()

    assert resolved is not None
    credentials, source = resolved
    assert credentials == AwsCredentials("FILEKEY", "filesecret")
    assert source == "AWS profile 'default'"
