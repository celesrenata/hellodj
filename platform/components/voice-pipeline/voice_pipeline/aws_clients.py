"""AWS client factory for the voice-pipeline's managed-AI dependencies.

Bedrock (runtime), Transcribe, and Polly clients are created lazily via the
boto3 **default credential chain**, which resolves the pod's IAM task role at
runtime. There are NO static access keys anywhere in this component.

boto3 is imported lazily inside the factory so the module compiles and imports
cleanly in environments where boto3 is not installed (e.g. lint/compile CI).
For tests, an :class:`AwsClientFactory` can be constructed with pre-built
client objects (fakes/mocks) injected directly, avoiding any live AWS calls.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AwsClientFactory"]


class AwsClientFactory:
    """Creates (or holds injected) boto3 clients for AWS AI services.

    In production, pass only ``region`` and let the factory build clients via
    the boto3 default credential chain (IAM task role). In tests, inject
    ``bedrock``, ``transcribe``, and/or ``polly`` clients directly so no network
    or credentials are required.
    """

    def __init__(
        self,
        region: str,
        *,
        session: Any | None = None,
        bedrock: Any | None = None,
        transcribe: Any | None = None,
        polly: Any | None = None,
    ) -> None:
        """Initialise the factory.

        Args:
            region: AWS region for all created clients.
            session: Optional pre-built boto3 Session (mockable in tests).
            bedrock: Optional injected Bedrock runtime client (tests).
            transcribe: Optional injected Transcribe client (tests).
            polly: Optional injected Polly client (tests).
        """
        self._region = region
        self._session = session
        self._bedrock = bedrock
        self._transcribe = transcribe
        self._polly = polly

    @property
    def region(self) -> str:
        """The AWS region these clients target."""
        return self._region

    def _session_or_default(self) -> Any:
        """Return the injected session or build a default one lazily."""
        if self._session is not None:
            return self._session
        # Lazy import so the module imports without boto3 installed.
        import boto3  # noqa: PLC0415 - intentional lazy import

        self._session = boto3.session.Session(region_name=self._region)
        return self._session

    def bedrock_runtime(self) -> Any:
        """Return the Bedrock runtime client (injected or lazily created)."""
        if self._bedrock is None:
            self._bedrock = self._session_or_default().client(
                "bedrock-runtime", region_name=self._region
            )
        return self._bedrock

    def transcribe(self) -> Any:
        """Return the Transcribe streaming/batch client (injected or created)."""
        if self._transcribe is None:
            self._transcribe = self._session_or_default().client(
                "transcribe", region_name=self._region
            )
        return self._transcribe

    def polly(self) -> Any:
        """Return the Polly client (injected or lazily created)."""
        if self._polly is None:
            self._polly = self._session_or_default().client(
                "polly", region_name=self._region
            )
        return self._polly
