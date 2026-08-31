"""External employee identity providers for SemaRail.

Provider access tokens are used only to retrieve a verified profile. They are
never returned to a caller, persisted, or included in exceptions.
"""

from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

try:
    from .access_control import AccessControlError
except ImportError:  # pragma: no cover - direct module loading
    from access_control import AccessControlError  # type: ignore[no-redef]


MAX_IDENTITY_RESPONSE_BYTES = 1_048_576


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 2048:
        raise ValueError(f"identity provider {field_name} is invalid")
    return value.strip()


def _safe_endpoint(value: Any, field_name: str, *, allow_loopback_http: bool = False) -> str:
    endpoint = _required_text(value, field_name)
    parsed = urlsplit(endpoint)
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.hostname
        or (parsed.scheme != "https" and not (allow_loopback_http and parsed.scheme == "http" and loopback))
    ):
        raise ValueError(f"identity provider {field_name} must be a secure URL")
    return endpoint


def _optional_text(value: Any, *, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _identity_key(*parts: str) -> str:
    """Return an immutable provider-instance key without exposing configuration."""

    material = "\0".join(parts).encode("utf-8")
    return f"idp_{hashlib.sha256(material).hexdigest()}"


class JsonTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


class UrlLibJsonTransport:
    """Small bounded HTTPS JSON client with deliberately generic failures."""

    def request(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        if json_body is not None and form_body is not None:
            raise ValueError("identity request body is ambiguous")
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(dict(json_body), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urlencode(dict(form_body)).encode("ascii")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - endpoints are trusted configuration
                raw = response.read(MAX_IDENTITY_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider request failed", status=502) from exc
        if len(raw) > MAX_IDENTITY_RESPONSE_BYTES:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider response is too large", status=502)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider returned invalid JSON", status=502) from exc
        if not isinstance(decoded, Mapping):
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider returned invalid JSON", status=502)
        return dict(decoded)


@dataclass(frozen=True)
class IdentityProfile:
    external_subject: str
    name: str
    organization_external_id: str | None = None
    profile: Mapping[str, Any] = field(default_factory=dict)


class IdentityProvider(Protocol):
    id: str
    label: str
    redirect_uri: str
    organization_id: str
    identity_key: str

    def authorization_url(self, state: str) -> str: ...

    def exchange(self, code: str) -> IdentityProfile: ...


@dataclass(frozen=True)
class GenericOidcProvider:
    id: str
    label: str
    client_id: str
    client_secret: str = field(repr=False)
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    userinfo_endpoint: str = ""
    redirect_uri: str = ""
    organization_id: str = "default"
    allowed_organization_external_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    subject_claim: str = "sub"
    name_claim: str = "name"
    organization_claim: str | None = None
    employee_number_claim: str | None = "employee_number"
    transport: JsonTransport = field(default_factory=UrlLibJsonTransport, repr=False, compare=False)

    @property
    def identity_key(self) -> str:
        return _identity_key(
            "oidc",
            self.client_id,
            self.authorization_endpoint,
            self.token_endpoint,
            self.userinfo_endpoint,
            self.subject_claim,
            self.organization_claim or "",
            *sorted(self.allowed_organization_external_ids),
        )

    def authorization_url(self, state: str) -> str:
        return f"{self.authorization_endpoint}?{urlencode({'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'scope': ' '.join(self.scopes), 'state': state})}"

    def exchange(self, code: str) -> IdentityProfile:
        token = self.transport.request(
            self.token_endpoint,
            method="POST",
            form_body={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            },
        )
        access_token = _optional_text(token.get("access_token"), limit=4096)
        if access_token is None:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider did not issue an access token", status=502)
        claims = self.transport.request(
            self.userinfo_endpoint,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        subject = _optional_text(claims.get(self.subject_claim))
        if subject is None:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "identity provider did not return a subject", status=502)
        name = _optional_text(claims.get(self.name_claim)) or subject
        organization = _optional_text(claims.get(self.organization_claim)) if self.organization_claim else None
        if self.allowed_organization_external_ids and organization not in self.allowed_organization_external_ids:
            raise AccessControlError("ORGANIZATION_NOT_ALLOWED", "identity organization is not allowed", status=403)
        profile = {
            key: value
            for key, value in {
                "email": _optional_text(claims.get("email")),
                "employeeNumber": _optional_text(claims.get(self.employee_number_claim)) if self.employee_number_claim else None,
                "avatarUrl": _optional_text(claims.get("picture"), limit=2048),
            }.items()
            if value is not None
        }
        return IdentityProfile(subject, name, organization, profile)


@dataclass(frozen=True)
class DingTalkProvider:
    id: str
    label: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str = ""
    organization_id: str = "default"
    allowed_organization_external_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ("openid", "corpid", "Contact.User.Read")
    transport: JsonTransport = field(default_factory=UrlLibJsonTransport, repr=False, compare=False)

    AUTHORIZATION_ENDPOINT = "https://login.dingtalk.com/oauth2/auth"
    TOKEN_ENDPOINT = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
    PROFILE_ENDPOINT = "https://api.dingtalk.com/v1.0/contact/users/me"

    @property
    def identity_key(self) -> str:
        return _identity_key("dingtalk", self.client_id, *sorted(self.allowed_organization_external_ids))

    def authorization_url(self, state: str) -> str:
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode({'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'response_type': 'code', 'prompt': 'consent', 'scope': ' '.join(self.scopes), 'state': state})}"

    def exchange(self, code: str) -> IdentityProfile:
        token = self.transport.request(
            self.TOKEN_ENDPOINT,
            method="POST",
            json_body={
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
                "code": code,
                "grantType": "authorization_code",
            },
        )
        access_token = _optional_text(token.get("accessToken"), limit=4096)
        if access_token is None:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "DingTalk did not issue an access token", status=502)
        profile = self.transport.request(
            self.PROFILE_ENDPOINT,
            method="GET",
            headers={"x-acs-dingtalk-access-token": access_token},
        )
        external_subject = _optional_text(profile.get("unionId"))
        if external_subject is None:
            raise AccessControlError("IDENTITY_PROVIDER_FAILED", "DingTalk did not return a stable user identity", status=502)
        name = _optional_text(profile.get("nick")) or external_subject
        organization = _optional_text(token.get("corpId"))
        if organization not in self.allowed_organization_external_ids:
            raise AccessControlError("ORGANIZATION_NOT_ALLOWED", "DingTalk organization is not allowed", status=403)
        safe_profile = {
            key: value
            for key, value in {
                "openId": _optional_text(profile.get("openId")),
                "avatarUrl": _optional_text(profile.get("avatarUrl"), limit=2048),
            }.items()
            if value is not None
        }
        return IdentityProfile(external_subject, name, organization, safe_profile)


class IdentityProviderRegistry:
    def __init__(self, providers: Mapping[str, IdentityProvider] | None = None) -> None:
        self._providers = dict(providers or {})

    @classmethod
    def from_environment(cls) -> "IdentityProviderRegistry":
        raw = os.environ.get("SEMARAIL_IDENTITY_PROVIDERS", "").strip()
        if not raw:
            return cls()
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SEMARAIL_IDENTITY_PROVIDERS must be valid JSON") from exc
        if not isinstance(config, Mapping):
            raise ValueError("SEMARAIL_IDENTITY_PROVIDERS must be a JSON object")
        providers: dict[str, IdentityProvider] = {}
        for provider_id, item in config.items():
            if not isinstance(provider_id, str) or not isinstance(item, Mapping):
                raise ValueError("identity provider configuration is invalid")
            kind = item.get("type")
            common = {
                "id": _required_text(provider_id, "id"),
                "label": _required_text(item.get("label", provider_id), "label"),
                "client_id": _required_text(item.get("clientId"), "clientId"),
                "client_secret": _required_text(item.get("clientSecret"), "clientSecret"),
                "redirect_uri": _safe_endpoint(item.get("redirectUri"), "redirectUri", allow_loopback_http=True),
                "organization_id": _required_text(item.get("organizationId", "default"), "organizationId"),
            }
            scopes_raw = item.get("scopes")
            scopes = tuple(str(value).strip() for value in scopes_raw if isinstance(value, str) and value.strip()) if isinstance(scopes_raw, list) else ()
            allowed_organizations_raw = item.get("allowedOrganizationExternalIds")
            allowed_organizations = (
                tuple(_required_text(value, "allowedOrganizationExternalIds") for value in allowed_organizations_raw)
                if isinstance(allowed_organizations_raw, list)
                else ()
            )
            if kind == "dingtalk":
                if len(allowed_organizations) != 1:
                    raise ValueError("DingTalk identity providers require exactly one allowedOrganizationExternalId")
                effective_scopes = scopes or ("openid", "corpid", "Contact.User.Read")
                if not {"openid", "corpid", "Contact.User.Read"}.issubset(effective_scopes):
                    raise ValueError(
                        "DingTalk identity provider scopes must include openid, corpid, and Contact.User.Read"
                    )
                providers[provider_id] = DingTalkProvider(
                    **common,
                    scopes=effective_scopes,
                    allowed_organization_external_ids=allowed_organizations,
                )
            elif kind == "oidc":
                organization_claim = _optional_text(item.get("organizationClaim"))
                single_tenant_issuer = item.get("singleTenantIssuer") is True
                if item.get("singleTenantIssuer") not in {None, True, False}:
                    raise ValueError("OIDC singleTenantIssuer must be a boolean")
                if single_tenant_issuer and (organization_claim or allowed_organizations):
                    raise ValueError("OIDC singleTenantIssuer cannot be combined with organization allowlisting")
                if not single_tenant_issuer and (
                    not organization_claim or len(allowed_organizations) != 1
                ):
                    raise ValueError(
                        "OIDC providers require a single-tenant issuer or exactly one allowed external organization"
                    )
                effective_scopes = scopes or ("openid", "profile", "email")
                if "openid" not in effective_scopes:
                    raise ValueError("OIDC identity provider scopes must include openid")
                providers[provider_id] = GenericOidcProvider(
                    **common,
                    authorization_endpoint=_safe_endpoint(item.get("authorizationEndpoint"), "authorizationEndpoint"),
                    token_endpoint=_safe_endpoint(item.get("tokenEndpoint"), "tokenEndpoint"),
                    userinfo_endpoint=_safe_endpoint(item.get("userinfoEndpoint"), "userinfoEndpoint"),
                    scopes=effective_scopes,
                    allowed_organization_external_ids=allowed_organizations,
                    subject_claim=_required_text(item.get("subjectClaim", "sub"), "subjectClaim"),
                    name_claim=_required_text(item.get("nameClaim", "name"), "nameClaim"),
                    organization_claim=organization_claim,
                    employee_number_claim=_optional_text(item.get("employeeNumberClaim", "employee_number")),
                )
            else:
                raise ValueError("identity provider type is unsupported")
        return cls(providers)

    def get(self, provider_id: str) -> IdentityProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            raise AccessControlError("PROVIDER_NOT_FOUND", "identity provider was not found", status=404)
        return provider

    def public_items(self) -> list[dict[str, str]]:
        return [{"id": provider.id, "label": provider.label} for provider in self._providers.values()]


__all__ = [
    "DingTalkProvider", "GenericOidcProvider", "IdentityProfile", "IdentityProvider",
    "IdentityProviderRegistry", "JsonTransport", "UrlLibJsonTransport",
]
