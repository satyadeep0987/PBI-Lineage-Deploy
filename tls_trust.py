"""Configure HTTPS certificate trust before HTTP client libraries are imported."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _resolved_bundle_path(raw_path):
    path = Path(str(raw_path or "").strip()).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def configure_tls_trust():
    """Use an explicit CA bundle or the operating system certificate store."""
    pbi_bundle = str(os.getenv("PBI_CA_BUNDLE") or "").strip()
    requests_bundle = str(os.getenv("REQUESTS_CA_BUNDLE") or "").strip()
    curl_bundle = str(os.getenv("CURL_CA_BUNDLE") or "").strip()
    configured_bundle = pbi_bundle or requests_bundle or curl_bundle

    if configured_bundle:
        bundle_path = _resolved_bundle_path(configured_bundle)
        if pbi_bundle:
            os.environ["REQUESTS_CA_BUNDLE"] = str(bundle_path)
        if not bundle_path.is_file():
            return {
                "mode": "invalid_ca_bundle",
                "ca_bundle": str(bundle_path),
                "error": f"Configured CA bundle does not exist: {bundle_path}",
            }
        return {
            "mode": "custom_ca_bundle",
            "ca_bundle": str(bundle_path),
            "error": None,
        }

    try:
        import truststore

        truststore.inject_into_ssl()
        return {
            "mode": "system_trust_store",
            "ca_bundle": None,
            "error": None,
        }
    except ImportError:
        return {
            "mode": "python_default_ca_bundle",
            "ca_bundle": None,
            "error": "The optional truststore package is not installed.",
        }
    except Exception as exc:
        return {
            "mode": "python_default_ca_bundle",
            "ca_bundle": None,
            "error": f"Could not initialize the operating system trust store: {exc}",
        }


def is_tls_certificate_error(error):
    message = f"{type(error).__name__}: {error}".lower()
    return any(
        marker in message
        for marker in (
            "sslerror",
            "certificate_verify_failed",
            "certificate verify failed",
            "unable to get local issuer certificate",
            "ca certificate bundle",
        )
    )


def format_request_exception(error, trust_config=None):
    """Return concise remediation when an HTTPS request cannot build a trust chain."""
    config = trust_config or {}
    mode = str(config.get("mode") or "unknown")
    if not is_tls_certificate_error(error) and mode != "invalid_ca_bundle":
        return str(error)

    host_match = re.search(r"host='([^']+)'", str(error), flags=re.IGNORECASE)
    host_detail = f" for {host_match.group(1)}" if host_match else ""

    if mode == "invalid_ca_bundle":
        remediation = str(config.get("error") or "The configured CA bundle is invalid.")
    elif mode == "python_default_ca_bundle":
        remediation = (
            "Install the project requirements so truststore can use the operating system certificates. "
            "Alternatively set PBI_CA_BUNDLE to a valid PEM CA-chain file."
        )
    else:
        remediation = (
            "Install the organization's proxy/root CA in the operating system Trusted Root store, "
            "or set PBI_CA_BUNDLE to a valid PEM CA-chain file."
        )

    return (
        f"TLS certificate verification failed{host_detail}. {remediation} "
        f"Active trust mode: {mode}. SSL verification was not disabled."
    )
