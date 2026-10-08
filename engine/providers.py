"""Fontes legítimas; nenhuma integração externa é simulada como real."""
import csv
import io
from typing import Protocol


class LeadSource(Protocol):
    def collect(self, payload: str) -> list[dict]: ...


class CSVProvider:
    def collect(self, payload):
        reader = csv.DictReader(io.StringIO(payload.lstrip('\ufeff')))
        if not reader.fieldnames or 'company' not in reader.fieldnames:
            raise ValueError('CSV precisa de cabeçalho company.')
        rows = list(reader)
        if len(rows) > 1000:
            raise ValueError('Limite de 1000 linhas por importação.')
        return rows


class ManualProvider:
    def collect(self, payload):
        return [payload]


class UnavailableProvider:
    def collect(self, payload):
        raise ValueError('Integração indisponível: configure um adapter autorizado antes de usar.')


PROVIDERS = {'csv': CSVProvider(), 'manual': ManualProvider(),
             **{name: UnavailableProvider() for name in ('instagram', 'google', 'maps', 'linkedin', 'website')}}
