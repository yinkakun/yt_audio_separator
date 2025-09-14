from typing import Optional
from services.audio_processor import DefaultSeparatorProvider


class _SeparatorProviderState:
    def __init__(self):
        self._provider: Optional[DefaultSeparatorProvider] = None

    def set_provider(self, provider: DefaultSeparatorProvider) -> None:
        self._provider = provider

    def get_provider(self) -> Optional[DefaultSeparatorProvider]:
        return self._provider


_state = _SeparatorProviderState()


def set_global_separator_provider(provider: DefaultSeparatorProvider) -> None:
    _state.set_provider(provider)


def get_global_separator_provider() -> Optional[DefaultSeparatorProvider]:
    return _state.get_provider()
