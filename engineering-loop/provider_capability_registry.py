"""Pure immutable closed-world provider capability registrations."""

from dataclasses import dataclass
import re


class ProviderCapabilityRegistryError(Exception):
    pass


class InvalidCapabilityDefinitionError(
    ProviderCapabilityRegistryError
):
    pass


class ClosedWorldRegistrationError(ProviderCapabilityRegistryError):
    pass


class UnknownCapabilityError(ProviderCapabilityRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    operation: str
    capability_version: int


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    implementation_id: str
    implementation_version: int


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    capability: CapabilityDefinition
    provider: ProviderDescriptor


@dataclass(frozen=True, slots=True)
class ResolvedProviderCapability:
    capability_id: str
    capability: CapabilityDefinition
    provider: ProviderDescriptor


@dataclass(frozen=True, slots=True)
class ProviderCapabilityRegistry:
    registrations: tuple

    @property
    def capability_ids(self):
        return tuple(
            item.capability.capability_id
            for item in self.registrations
        )

    def resolve(self, capability_id):
        if type(capability_id) is not str:
            raise UnknownCapabilityError(
                "canonical capability identifier required"
            )
        matches = tuple(
            item for item in self.registrations
            if item.capability.capability_id == capability_id
        )
        if len(matches) != 1:
            raise UnknownCapabilityError(
                "capability is not explicitly registered"
            )
        registration = matches[0]
        return ResolvedProviderCapability(
            capability_id,
            registration.capability,
            registration.provider,
        )

    def require_registration(self, provider_id, operation):
        if type(provider_id) is not str or type(operation) is not str:
            raise UnknownCapabilityError(
                "exact provider and operation required"
            )
        matches = tuple(
            item for item in self.registrations
            if (
                item.provider.provider_id == provider_id
                and item.capability.operation == operation
            )
        )
        if len(matches) != 1:
            raise UnknownCapabilityError(
                "provider operation is not explicitly registered"
            )
        item = matches[0]
        return ResolvedProviderCapability(
            item.capability.capability_id,
            item.capability,
            item.provider,
        )


_CAPABILITY_ID = re.compile(
    r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+\Z"
)
_COMPONENT_ID = re.compile(
    r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z"
)
_OPERATION = re.compile(
    r"[a-z][a-z0-9]*(?: [a-z0-9]+)*\Z"
)


def _validate_registration(registration):
    if type(registration) is not ProviderRegistration:
        raise ClosedWorldRegistrationError(
            "exact ProviderRegistration required"
        )
    capability = registration.capability
    provider = registration.provider
    if (
        type(capability) is not CapabilityDefinition
        or type(capability.capability_id) is not str
        or _CAPABILITY_ID.fullmatch(capability.capability_id) is None
        or type(capability.operation) is not str
        or _OPERATION.fullmatch(capability.operation) is None
        or type(capability.capability_version) is not int
        or capability.capability_version < 1
        or type(provider) is not ProviderDescriptor
        or type(provider.provider_id) is not str
        or _COMPONENT_ID.fullmatch(provider.provider_id) is None
        or type(provider.implementation_id) is not str
        or _COMPONENT_ID.fullmatch(provider.implementation_id) is None
        or type(provider.implementation_version) is not int
        or provider.implementation_version < 1
    ):
        raise InvalidCapabilityDefinitionError(
            "invalid closed provider capability registration"
        )
    return registration


def build_provider_capability_registry(registrations):
    if type(registrations) is not tuple:
        raise ClosedWorldRegistrationError(
            "closed registration tuple required"
        )
    validated = tuple(
        _validate_registration(item) for item in registrations
    )
    capability_ids = tuple(
        item.capability.capability_id for item in validated
    )
    provider_operations = tuple(
        (item.provider.provider_id, item.capability.operation)
        for item in validated
    )
    if (
        len(set(capability_ids)) != len(capability_ids)
        or len(set(provider_operations)) != len(provider_operations)
    ):
        raise ClosedWorldRegistrationError(
            "capabilities and provider operations must be unique"
        )
    ordered = tuple(sorted(
        validated,
        key=lambda item: item.capability.capability_id,
    ))
    return ProviderCapabilityRegistry(ordered)


def default_provider_capability_registry():
    return build_provider_capability_registry((
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.repository.mutation.v1",
                "apply approved repository mutation contract",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.repository.product.routing.patch.v1",
                "apply authorized nexus product routing patch",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.software.fixture.repair.v1",
                "nexus fixture repair",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "np1.governed.corrective.write.v1",
                "np1 corrective write",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "np1.governed.corrective.multi-artifact.publish.v1",
                "np1 atomic artifact set publish",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "np1.fcr004.preservation.verify.v1",
                "fcr004 preservation verify",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "external.providers.health.v1",
                "providers health",
                1,
            ),
            ProviderDescriptor(
                "local-supervised-bridge",
                "governed-external-execution-adapter",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.llm.code.proposal.simple.v1",
                "generate governed code proposal simple",
                1,
            ),
            ProviderDescriptor(
                "ollama-qwen-7b",
                "governed-llm-execution-adapter-ollama-7b",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.llm.code.proposal.medium.v1",
                "generate governed code proposal medium",
                1,
            ),
            ProviderDescriptor(
                "ollama-qwen-14b",
                "governed-llm-execution-adapter-ollama-14b",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.llm.code.proposal.escalated.v1",
                "generate escalated governed code proposal",
                1,
            ),
            ProviderDescriptor(
                "nvidia-nim",
                "governed-llm-execution-adapter-nim",
                1,
            ),
        ),
        ProviderRegistration(
            CapabilityDefinition(
                "nexus.llm.reasoning.night-senior.v1",
                "generate governed reasoning review",
                1,
            ),
            ProviderDescriptor(
                "ollama-qwen-night-senior",
                "governed-llm-execution-adapter-ollama-night-senior",
                1,
            ),
        ),
    ))


__all__ = (
    "CapabilityDefinition", "ProviderDescriptor",
    "ProviderRegistration", "ResolvedProviderCapability",
    "ProviderCapabilityRegistry",
    "build_provider_capability_registry",
    "default_provider_capability_registry",
    "ProviderCapabilityRegistryError",
    "InvalidCapabilityDefinitionError",
    "ClosedWorldRegistrationError", "UnknownCapabilityError",
)
