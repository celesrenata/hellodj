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
        ai_task_role_arn: str = "",
    ) -> None:
        """Initialise the factory.

        Args:
            region: AWS region for all created clients.
            session: Optional pre-built boto3 Session (mockable in tests).
            bedrock: Optional injected Bedrock runtime client (tests).
            transcribe: Optional injected Transcribe client (tests).
            polly: Optional injected Polly client (tests).
            ai_task_role_arn: ARN of the keyless Bedrock/Transcribe/Polly task
                role to assume before building clients. When set (production),
                the factory exchanges the pod's own IRSA credentials for the
                dedicated AI task role via STS ``AssumeRole`` — the pod role is
                only granted ``sts:AssumeRole`` on this ARN, so the assume is
                what actually authorizes Bedrock/Transcribe/Polly. When empty
                (tests / injected clients / local dev) the default credential
                chain is used directly.
        """
        self._region = region
        self._session = session
        self._bedrock = bedrock
        self._transcribe = transcribe
        self._polly = polly
        self._ai_task_role_arn = ai_task_role_arn

    @property
    def region(self) -> str:
        """The AWS region these clients target."""
        return self._region

    def _session_or_default(self) -> Any:
        """Return the injected session, or lazily build one.

        When an ``ai_task_role_arn`` is configured and no session was injected,
        the built session is bound to STS-assumed AI-task-role credentials so
        every client this factory creates speaks Bedrock/Transcribe/Polly under
        that dedicated keyless role. Any assume-role failure falls back to the
        default credential chain (degrade rather than crash the pod).
        """
        if self._session is not None:
            return self._session
        # Lazy import so the module imports without boto3 installed.
        import boto3  # noqa: PLC0415 - intentional lazy import

        if self._ai_task_role_arn:
            assumed = self._assume_ai_task_role(boto3)
            if assumed is not None:
                self._session = assumed
                return self._session
        self._session = boto3.session.Session(region_name=self._region)
        return self._session

    def _assume_ai_task_role(self, boto3: Any) -> Any | None:
        """Return a boto3 Session bound to the assumed AI task role, or None."""
        try:
            sts = boto3.session.Session(region_name=self._region).client("sts")
            creds = sts.assume_role(
                RoleArn=self._ai_task_role_arn,
                RoleSessionName="voice-pipeline-ai",
            ).get("Credentials")
            if not creds:
                return None
            return boto3.session.Session(
                region_name=self._region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        except Exception:  # noqa: BLE001 - degrade to the default chain
            return None

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
