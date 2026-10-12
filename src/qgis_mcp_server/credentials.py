"""Provider API keys: where they come from, and how their absence is reported.

``spec/design.md`` §6.3: OS Data Hub and OpenTopography require free
registration for an API key, and the server cannot complete a signup. So the
job here is to find a key if one is configured, and otherwise to say exactly
what is missing and where to go for it.

**Key values never leave this module.** A credential's *status* travels --
present or absent, which source supplied it, how long it is -- and the value
itself goes only to the adapter making the request. Not into a tool response,
not into a log line, not into an error message. The length is reported because
it distinguishes a real key from an empty string left in a config file, which
is a common and otherwise invisible mistake; no prefix or suffix is reported,
because a partial key is still a leaked key and the convenience is not worth
it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

#: Environment variable prefix for every credential.
ENV_PREFIX = "QGIS_MCP_"


@dataclass(frozen=True)
class CredentialSpec:
    """Where one provider's key lives, and what to do when it is missing."""

    #: Provider name, matching the adapter's capability declaration.
    provider: str
    #: Key within the ``[credentials]`` table of ``qgis-mcp.toml``.
    config_key: str
    #: Environment variable, which takes precedence over the config file.
    env_var: str
    #: Where to register. Reported verbatim when the key is absent, since
    #: "missing credential" is only actionable with somewhere to go.
    registration_url: str
    #: Anything a first-time registrant needs that the URL does not convey --
    #: quotas, which sub-APIs to enable, licensing restrictions.
    notes: str = ""


@dataclass(frozen=True)
class CredentialStatus:
    """Whether a key is available, and from where. Never the key itself."""

    provider: str
    present: bool
    source: str | None = None
    length: int | None = None
    registration_url: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "present": self.present,
        }
        if self.present:
            payload["source"] = self.source
            payload["key_length"] = self.length
        else:
            payload["registration_url"] = self.registration_url
            payload["action"] = (
                f"register at {self.registration_url}, then set "
                f"{ENV_PREFIX}{self.provider.upper()}_KEY or add it to the "
                f"[credentials] table in qgis-mcp.toml"
            )
        if self.notes:
            payload["notes"] = self.notes
        return payload


@dataclass
class CredentialStore:
    """Resolves provider keys from the environment and the config file.

    The environment wins over the config file, so a key can be supplied for one
    run without editing anything -- and so CI can inject one without a file on
    disk.
    """

    #: The ``[credentials]`` table from ``qgis-mcp.toml``, if any.
    config_values: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.config_values = dict(self.config_values or {})

    def get(self, spec: CredentialSpec) -> str | None:
        """The key for ``spec``, or ``None``.

        A blank or whitespace-only value counts as absent. A config file with
        ``os_data_hub = ""`` in it is a key that was meant to be filled in and
        was not, and treating it as present would turn a clear "no credential"
        into an authentication failure from the provider.
        """
        from_env = os.environ.get(spec.env_var)
        if from_env and from_env.strip():
            return from_env.strip()
        from_config = self.config_values.get(spec.config_key)
        if isinstance(from_config, str) and from_config.strip():
            return from_config.strip()
        return None

    def status(self, spec: CredentialSpec) -> CredentialStatus:
        """Report availability without disclosing the value."""
        from_env = os.environ.get(spec.env_var)
        if from_env and from_env.strip():
            value = from_env.strip()
            return CredentialStatus(
                provider=spec.provider,
                present=True,
                source=f"environment variable {spec.env_var}",
                length=len(value),
                notes=spec.notes,
            )

        from_config = self.config_values.get(spec.config_key)
        if isinstance(from_config, str) and from_config.strip():
            return CredentialStatus(
                provider=spec.provider,
                present=True,
                source=f"qgis-mcp.toml [credentials] {spec.config_key}",
                length=len(from_config.strip()),
                notes=spec.notes,
            )

        return CredentialStatus(
            provider=spec.provider,
            present=False,
            registration_url=spec.registration_url,
            notes=spec.notes,
        )


__all__ = [
    "ENV_PREFIX",
    "CredentialSpec",
    "CredentialStatus",
    "CredentialStore",
]
