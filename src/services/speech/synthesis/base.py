from abc import ABC, abstractmethod

class BaseSynthesisService(ABC):
    @abstractmethod
    def synthesize(self, text: str):
        pass
