"""The bench's openJII sign-in and its SigV4 presigning.

The presign is the part with no forgiving failure mode: AWS answers a wrong
signature with a bare 403 on the WebSocket upgrade, minutes after a
calibration that cannot be repeated cheaply. The signature here is pinned
against a vector cross-checked with botocore's own SigV4 signer, so a change
to the canonical request breaks a test rather than a bench session.
"""

import datetime as dt
import json
import os
import sys
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openjii_auth as oj


HOST = "a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com"
WHEN = dt.datetime(2026, 8, 26, 9, 30, 0, tzinfo=dt.timezone.utc)
#: botocore SigV4Auth("iotdevicegateway", "eu-central-1") over the same
#: canonical request produces exactly this.
EXPECTED_SIG = "2f7435058bf2f8614191535c2489269e96f83f63c122030a057a5767337796fe"


def creds(token=""):
    return oj.AwsCredentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        session_token=token,
        expiration=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))


def query_of(path):
    head, _, query = path.partition("?")
    assert head == "/mqtt"
    return urllib.parse.parse_qs(query, strict_parsing=True)


def test_signature_matches_the_pinned_vector():
    q = query_of(oj.presign_iot_wss_path(HOST, creds(), now=WHEN))
    assert q["X-Amz-Signature"] == [EXPECTED_SIG]
    assert q["X-Amz-Credential"] == [
        "AKIDEXAMPLE/20260826/eu-central-1/iotdevicegateway/aws4_request"]
    assert q["X-Amz-Date"] == ["20260826T093000Z"]
    assert q["X-Amz-SignedHeaders"] == ["host"]


def test_credential_scope_slashes_are_percent_encoded():
    # urlencode's default safe="/" would leave them bare and sign a string
    # AWS never recomputes - a 403 on the upgrade, nothing more diagnostic.
    path = oj.presign_iot_wss_path(HOST, creds(), now=WHEN)
    raw = path.split("X-Amz-Credential=")[1].split("&")[0]
    assert "%2F" in raw and "/" not in raw


def test_session_token_is_appended_after_signing():
    """AWS IoT excludes X-Amz-Security-Token from the canonical query."""
    token = "IQoJb3JpZ2luX2Vj//////wEaX+/token=="
    with_token = oj.presign_iot_wss_path(HOST, creds(token), now=WHEN)
    without = oj.presign_iot_wss_path(HOST, creds(), now=WHEN)
    q = query_of(with_token)
    assert q["X-Amz-Security-Token"] == [token]
    assert q["X-Amz-Signature"] == query_of(without)["X-Amz-Signature"]


def test_region_comes_from_the_endpoint():
    assert oj.environment("prod").region == "eu-central-1"
    with pytest.raises(oj.OpenJIIError):
        oj._region_from_host("mqtt.example.com")


def test_unknown_environment_names_the_valid_ones():
    with pytest.raises(oj.OpenJIIError) as exc:
        oj.environment("staging")
    assert "prod" in str(exc.value)


def test_credentials_expiry_is_pessimistic_when_unreadable():
    """An unparseable expiry must not read as "good for hours"."""
    payload = {"accessKeyId": "A", "secretAccessKey": "S", "sessionToken": "T",
               "expiration": "not-a-date"}
    fresh = oj.AwsCredentials.from_api(payload)
    assert fresh.valid()
    assert (fresh.expiration - dt.datetime.now(dt.timezone.utc)) < dt.timedelta(hours=1)


def test_credentials_inside_the_margin_are_not_valid():
    nearly = oj.AwsCredentials("A", "S", "T",
                               dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30))
    assert not nearly.valid()
    assert nearly.valid(margin_s=5)


def test_incomplete_credentials_are_refused():
    with pytest.raises(oj.OpenJIIError) as exc:
        oj.AwsCredentials.from_api({"accessKeyId": "A"})
    assert "secretAccessKey" in str(exc.value)


def test_settings_roundtrip_and_unknown_keys(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(oj, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(oj, "SETTINGS_FILE", path)

    settings = oj.Settings()
    settings.set_api_key("prod", "jii_abc")
    settings.environment = "prod"
    settings.save()

    # A field this version does not know must not blow up the next load.
    blob = json.loads(path.read_text(encoding="utf-8"))
    blob["some_future_field"] = 1
    path.write_text(json.dumps(blob), encoding="utf-8")

    again = oj.Settings.load()
    assert again.api_key("prod") == "jii_abc"
    assert again.api_key("dev") == ""
    assert again.environment == "prod"


def test_missing_settings_file_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(oj, "SETTINGS_FILE", tmp_path / "nope.json")
    assert oj.Settings.load().environment == "prod"


def test_flash_gui_key_is_read_not_written(tmp_path, monkeypatch):
    flash_dir = tmp_path / oj.FLASH_GUI_APP_NAME
    flash_dir.mkdir()
    (flash_dir / "settings.json").write_text(
        json.dumps({"api_keys": {"dev": "jii_from_flash_gui"}}), encoding="utf-8")
    monkeypatch.setattr(oj, "_config_dir",
                        lambda app_name=oj.APP_NAME: tmp_path / app_name)

    assert oj.flash_gui_api_key("dev") == "jii_from_flash_gui"
    assert oj.flash_gui_api_key("prod") == ""
    # Unchanged on disk: that tool owns its own settings file.
    assert json.loads((flash_dir / "settings.json").read_text(
        encoding="utf-8")) == {"api_keys": {"dev": "jii_from_flash_gui"}}


def test_flash_gui_key_survives_a_corrupt_file(tmp_path, monkeypatch):
    flash_dir = tmp_path / oj.FLASH_GUI_APP_NAME
    flash_dir.mkdir()
    (flash_dir / "settings.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(oj, "_config_dir",
                        lambda app_name=oj.APP_NAME: tmp_path / app_name)
    assert oj.flash_gui_api_key("dev") == ""


def test_validate_key_rejects_a_null_session():
    """better-auth answers 200 with a null body when unauthenticated."""
    client = oj.OpenJIIClient(oj.environment("prod"), "jii_stale")
    client._request = lambda *a, **k: (200, None)
    with pytest.raises(oj.OpenJIIError) as exc:
        client.validate_key()
    assert "api-keys" in str(exc.value)


def test_iot_credentials_are_cached_until_they_near_expiry():
    client = oj.OpenJIIClient(oj.environment("prod"), "jii_ok")
    calls = []

    def fake(method, path, body=None, timeout=30.0):
        calls.append(path)
        return 200, {"accessKeyId": "A", "secretAccessKey": "S",
                     "sessionToken": "T",
                     "expiration": (dt.datetime.now(dt.timezone.utc)
                                    + dt.timedelta(hours=1)).isoformat()}

    client._request = fake
    first = client.iot_credentials()
    assert client.iot_credentials() is first
    assert len(calls) == 1
    client.iot_credentials(refresh=True)
    assert len(calls) == 2


def test_iot_credentials_403_explains_the_feature_flag():
    client = oj.OpenJIIClient(oj.environment("prod"), "jii_ok")
    client._request = lambda *a, **k: (403, {"message": "forbidden"})
    with pytest.raises(oj.OpenJIIError) as exc:
        client.iot_credentials()
    assert "iot-devices" in str(exc.value)


def test_signed_in_client_without_a_key_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(oj, "SETTINGS_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(oj, "flash_gui_api_key", lambda env_key: "")
    with pytest.raises(oj.OpenJIIError) as exc:
        oj.signed_in_client("prod")
    assert "openjii.org" in str(exc.value)
