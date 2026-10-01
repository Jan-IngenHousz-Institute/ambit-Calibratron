"""openJII sign-in for the calibration bench, and the credentials to publish.

Why an API key and not a real login
-----------------------------------
openJII's better-auth config (``packages/auth/src/server.ts``) sets
``emailAndPassword: {enabled: false}``. Everything else it offers - emailOTP,
genericOAuth, passkey - ends in a browser session cookie that is httpOnly and
scoped to the web origin, which a desktop tool cannot read. What it does
enable is the apiKey plugin (``defaultPrefix: "jii_"``,
``enableSessionForAPIKeys: true``): an ``x-api-key`` header authenticates any
/api/v1 call as the owning user. So "sign in" here is: open the API-keys page
in the operator's browser, take one paste, validate it against
/api/v1/auth/get-session, and keep it for the bench.

Why temporary AWS credentials and not an X.509 certificate
----------------------------------------------------------
The bench is a host tool with an operator sitting at it, not a fielded board.
``GET /api/v1/iot/credentials`` hands the signed-in user short-lived Cognito
credentials, and the authenticated identity's policy (open-jii
``infrastructure/modules/cognito/main.tf``) already allows ``iot:Connect`` on
``client/*`` and ``iot:Publish`` on ``experiment/data_ingest/v1/*/*/*/*/*`` -
exactly the calibration topic. That leaves nothing on disk to lose and nothing
to rotate. Issuing a device certificate instead (POST
/api/v1/devices/{id}/credentials) would hit ``/rotate`` whenever one is
already live and silently revoke the copy every other bench publishes with.

The MQTT client id stays ``ambit_calibration_1`` so nothing downstream of the
ingest rule sees a change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP_NAME = "Calibratron"
USER_AGENT = "calibratron"

#: The tool whose stored key we offer to reuse: same operator, same account.
FLASH_GUI_APP_NAME = "ambyte-flash-gui"


class OpenJIIError(RuntimeError):
    """Auth or API failure, with a message meant for the operator."""


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Environment:
    key: str
    api_url: str
    web_url: str
    #: AWS IoT Core ATS data endpoint. Not derivable from any API - ops read it
    #: with ``aws iot describe-endpoint --endpoint-type iot:Data-ATS``.
    mqtt_host: str

    @property
    def api_keys_url(self) -> str:
        # Locale segment is required by the web app.
        return f"{self.web_url}/en-US/platform/account/api-keys"

    @property
    def region(self) -> str:
        return _region_from_host(self.mqtt_host)


ENVIRONMENTS: dict[str, Environment] = {
    "prod": Environment(
        key="prod",
        api_url="https://api.openjii.org",
        web_url="https://openjii.org",
        mqtt_host="a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com",
    ),
    "dev": Environment(
        key="dev",
        api_url="https://api.dev.openjii.org",
        web_url="https://dev.openjii.org",
        mqtt_host="a2s5vvyojsnl53-ats.iot.eu-central-1.amazonaws.com",
    ),
}


def environment(key: str) -> Environment:
    try:
        return ENVIRONMENTS[key]
    except KeyError:
        raise OpenJIIError(f"unknown openJII environment {key!r}; expected one "
                           f"of {', '.join(ENVIRONMENTS)}") from None


def _region_from_host(host: str) -> str:
    """``a3...-ats.iot.eu-central-1.amazonaws.com`` -> ``eu-central-1``."""
    parts = host.split(".")
    if len(parts) >= 3 and parts[1] == "iot":
        return parts[2]
    raise OpenJIIError(f"cannot read an AWS region out of the IoT endpoint {host!r}")


def _ssl_context() -> ssl.SSLContext:
    """Platform roots, plus certifi's when it is installed."""
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return context
    try:
        context.load_verify_locations(cafile=certifi.where())
    except OSError:
        pass
    return context


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass
class AwsCredentials:
    """Short-lived Cognito credentials for one bench session."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime

    @classmethod
    def from_api(cls, payload: dict) -> "AwsCredentials":
        missing = [k for k in ("accessKeyId", "secretAccessKey", "sessionToken")
                   if not payload.get(k)]
        if missing:
            raise OpenJIIError("openJII returned incomplete IoT credentials "
                               f"(missing {', '.join(missing)})")
        return cls(access_key_id=payload["accessKeyId"],
                   secret_access_key=payload["secretAccessKey"],
                   session_token=payload["sessionToken"],
                   expiration=_parse_expiry(payload.get("expiration")))

    def valid(self, margin_s: float = 120.0) -> bool:
        """True while more than ``margin_s`` is left: a publish that starts
        inside the margin would risk expiring mid-connect."""
        return (self.expiration
                - datetime.now(timezone.utc)).total_seconds() > margin_s


def _parse_expiry(value) -> datetime:
    """The API's ISO 8601 expiry, or a short pessimistic one.

    An unreadable expiry must never read as "valid for ages" - the margin in
    :meth:`AwsCredentials.valid` then forces an early refresh instead.
    """
    if not value:
        return datetime.now(timezone.utc) + timedelta(minutes=15)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc) + timedelta(minutes=15)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class OpenJIIClient:
    """The two endpoints the bench needs, over stdlib urllib."""

    def __init__(self, env: Environment, api_key: str):
        self.env = env
        self.api_key = (api_key or "").strip()
        self._creds: AwsCredentials | None = None
        self.user: dict | None = None

    # -- HTTP plumbing ------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: float = 30.0):
        url = f"{self.env.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                   "x-api-key": self.api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ssl_context()) as resp:
                raw = resp.read().decode("utf-8") or ""
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                parsed = json.loads(raw) if raw.strip() else None
            except ValueError:
                parsed = {"raw": raw[:400]}
            return exc.code, parsed
        except urllib.error.URLError as exc:
            raise OpenJIIError(f"cannot reach {self.env.api_url} ({exc.reason}). "
                               f"Check the network/VPN.") from exc

    @staticmethod
    def _error_text(payload) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "detail", "raw"):
                val = payload.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, dict) and isinstance(val.get("message"), str):
                    return val["message"]
        return json.dumps(payload)[:300] if payload is not None else "(no body)"

    # -- auth ---------------------------------------------------------------
    def validate_key(self) -> dict:
        """The signed-in user, or raise. better-auth answers 200 with a null
        body for an unauthenticated call, so 200 alone proves nothing."""
        if not self.api_key:
            raise OpenJIIError("no openJII API key set.")
        status, payload = self._request("GET", "/api/v1/auth/get-session",
                                        timeout=20)
        if status != 200 or not (isinstance(payload, dict) and payload.get("user")):
            raise OpenJIIError("the API key was rejected (or has expired). "
                               f"Create one at {self.env.api_keys_url} and "
                               f"paste it again.")
        self.user = payload["user"]
        return self.user

    def who(self) -> str:
        user = self.user or {}
        return user.get("email") or user.get("name") or "signed in"

    # -- publish credentials ------------------------------------------------
    def iot_credentials(self, *, refresh: bool = False) -> AwsCredentials:
        """Temporary AWS credentials for AWS IoT Core, cached until they near
        expiry: one run publishes once, but a bench session lasts hours."""
        if not refresh and self._creds is not None and self._creds.valid():
            return self._creds
        status, payload = self._request("GET", "/api/v1/iot/credentials")
        if status == 403:
            raise OpenJIIError("openJII refused IoT credentials (403). The "
                               "iot-devices feature flag is probably off for "
                               f"your account: {self._error_text(payload)}")
        if status != 200 or not isinstance(payload, dict):
            raise OpenJIIError(f"requesting IoT credentials failed ({status}): "
                               f"{self._error_text(payload)}")
        self._creds = AwsCredentials.from_api(payload)
        return self._creds


# ---------------------------------------------------------------------------
# SigV4 presigning for MQTT over WebSocket
# ---------------------------------------------------------------------------

_SERVICE = "iotdevicegateway"


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def presign_iot_wss_path(host: str, credentials: AwsCredentials, *,
                         region: str | None = None,
                         now: datetime | None = None) -> str:
    """The signed ``/mqtt?...`` path AWS IoT expects on a WebSocket upgrade.

    Plain SigV4 over a canonical GET of ``/mqtt``. The session token is
    deliberately appended *after* signing: AWS IoT excludes
    ``X-Amz-Security-Token`` from the canonical query string, and signing it
    in is the classic way to earn a 403 on the upgrade.
    """
    region = region or _region_from_host(host)
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{_SERVICE}/aws4_request"

    # safe="" matters: the scope in X-Amz-Credential is full of slashes, and
    # quote() would leave them bare, which signs a string AWS never recomputes.
    # Keys are already in the byte order SigV4 wants.
    query = urllib.parse.urlencode({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{credentials.access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-SignedHeaders": "host",
    }, safe="", quote_via=urllib.parse.quote)

    canonical_request = "\n".join((
        "GET", "/mqtt", query, f"host:{host}\n", "host",
        hashlib.sha256(b"").hexdigest()))
    string_to_sign = "\n".join((
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()))

    key = _sign(f"AWS4{credentials.secret_access_key}".encode("utf-8"), datestamp)
    for part in (region, _SERVICE, "aws4_request"):
        key = _sign(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    query += f"&X-Amz-Signature={signature}"
    if credentials.session_token:
        query += "&X-Amz-Security-Token=" + urllib.parse.quote(
            credentials.session_token, safe="")
    return f"/mqtt?{query}"


# ---------------------------------------------------------------------------
# Persisted settings
# ---------------------------------------------------------------------------

def _config_dir(app_name: str = APP_NAME) -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / app_name


CONFIG_DIR = _config_dir()
SETTINGS_FILE = CONFIG_DIR / "settings.json"


@dataclass
class Settings:
    """Bench settings that outlive the window. Keys only - no device data."""

    environment: str = "prod"
    api_keys: dict = field(default_factory=dict)     # env key -> "jii_..."

    @classmethod
    def load(cls) -> "Settings":
        try:
            blob = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in blob.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(self.__dict__, indent=2),
                                 encoding="utf-8")
        try:
            os.chmod(SETTINGS_FILE, 0o600)   # POSIX; Windows inherits profile ACLs
        except OSError:
            pass

    def api_key(self, env_key: str) -> str:
        return (self.api_keys or {}).get(env_key, "")

    def set_api_key(self, env_key: str, key: str) -> None:
        self.api_keys[env_key] = key


def flash_gui_api_key(env_key: str) -> str:
    """The key the ambyte flash GUI already stored for this environment.

    Same operator, same openJII account: offering it saves a second trip to
    the web UI. Never written back - that tool owns its own settings file.
    """
    path = _config_dir(FLASH_GUI_APP_NAME) / "settings.json"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    keys = blob.get("api_keys")
    return keys.get(env_key, "") if isinstance(keys, dict) else ""


def signed_in_client(env_key: str = "") -> OpenJIIClient:
    """A validated client from the stored key - for the CLI, which has no
    window to prompt in. Raises with what to do when there is no key."""
    settings = Settings.load()
    env = environment(env_key or settings.environment)
    key = settings.api_key(env.key) or flash_gui_api_key(env.key)
    if not key:
        raise OpenJIIError(
            f"not signed in to openJII ({env.key}). Create a personal API key "
            f"at {env.api_keys_url}, then either sign in once from the "
            f"Calibratron GUI, or store it with:\n"
            f"    python -c \"import openjii_auth as a; s=a.Settings.load(); "
            f"s.set_api_key('{env.key}', 'jii_...'); s.save()\"")
    client = OpenJIIClient(env, key)
    client.validate_key()
    return client
