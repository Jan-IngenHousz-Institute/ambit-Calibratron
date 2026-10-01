"""OpenJII GUI upload transport extracted from PR #4."""
import json
import logging

logger = logging.getLogger(__name__)

def publish_payload_mqtt5_wss(payload, topic, endpoint, credentials, *,
                              client_id="calibratron", port=443, qos=1,
                              timeout=10.0):
    """Publish over MQTT 5 on a SigV4-signed WebSocket (AWS IoT Core).

    The bench's route to openJII: ``credentials`` are the short-lived AWS
    credentials :meth:`openjii_auth.OpenJIIClient.iot_credentials` hands the
    signed-in operator, so no X.509 material is ever kept on the bench PC.
    ``client_id`` still travels to the ingest rule as ``clientid()``.
    """
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion
    except ImportError as exc:
        raise ImportError("publish_payload_mqtt5_wss needs paho-mqtt >= 2.0") from exc

    from openjii_auth import presign_iot_wss_path

    endpoint, port = _split_endpoint(endpoint, port)
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id=client_id, protocol=mqtt.MQTTv5,
                         transport="websockets")
    # Signed per connection: the path carries the whole SigV4 authorisation,
    # and a stale one is a 403 on the upgrade rather than a retryable error.
    client.ws_set_options(path=presign_iot_wss_path(endpoint, credentials))
    client.tls_set()
    return _mqtt5_deliver(client, payload, topic, endpoint, port, client_id,
                          qos=qos, timeout=timeout)


def _split_endpoint(endpoint, port):
    """``mqtts://host:8883/x`` -> ``("host", 8883)``; bare hosts keep ``port``."""
    endpoint = endpoint.strip()
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]
    endpoint = endpoint.split("/", 1)[0]
    if ":" in endpoint:
        host, _, maybe_port = endpoint.rpartition(":")
        if maybe_port.isdigit():
            endpoint, port = host, int(maybe_port)
    return endpoint, port


def _mqtt5_deliver(client, payload, topic, endpoint, port, client_id, *,
                   qos=1, timeout=10.0):
    """Connect an already-configured client, publish once, disconnect."""
    import threading

    body = payload if isinstance(payload, (bytes, bytearray, str)) else json.dumps(payload)
    connected, conn_state = threading.Event(), {}

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        conn_state["rc"] = reason_code
        connected.set()

    client.on_connect = _on_connect

    logger.info("MQTT5 connecting to %s:%d as %s ...", endpoint, port, client_id)
    client.connect(endpoint, port, keepalive=60)
    client.loop_start()
    try:
        if not connected.wait(timeout):
            raise TimeoutError(f"MQTT connect to {endpoint}:{port} timed out after {timeout}s")
        rc = conn_state.get("rc")
        if rc is not None and getattr(rc, "is_failure", False):
            raise ConnectionError(f"MQTT connect to {endpoint} rejected: {rc}")
        info = client.publish(topic, body, qos=qos)
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise TimeoutError(f"publish to {topic!r} not acknowledged within {timeout}s")
    finally:
        client.loop_stop()
        client.disconnect()

    logger.info("MQTT5 published %d bytes to %r",
                len(body if isinstance(body, (bytes, bytearray)) else body.encode()), topic)
    return True
