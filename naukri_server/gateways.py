"""Persistence gateways — abstract interfaces for data access."""

from abc import ABC, abstractmethod
from typing import Optional


class ApplicationGateway(ABC):
    """Interface for application data persistence."""

    @abstractmethod
    async def list_applications(self, status: str = None, date_from: str = None,
                                 date_to: str = None) -> list:
        pass

    @abstractmethod
    async def get_application(self, job_id: str) -> Optional[dict]:
        pass

    @abstractmethod
    async def save_application(self, app: dict) -> None:
        pass

    @abstractmethod
    async def delete_applications_before(self, date: str) -> int:
        pass


class JsonFileApplicationGateway(ApplicationGateway):
    """Concrete gateway — reads/writes applications.json."""

    def __init__(self, file_path, lock):
        self._file_path = file_path
        self._lock = lock

    async def list_applications(self, status=None, date_from=None, date_to=None):
        from naukri_server.tools.tracking import _load_json
        async with self._lock:
            apps = _load_json(self._file_path)
        if status:
            apps = [a for a in apps if a.get("status") == status]
        if date_from:
            apps = [a for a in apps if a.get("applied_at", "") >= date_from]
        if date_to:
            apps = [a for a in apps if a.get("applied_at", "") <= date_to]
        return apps

    async def get_application(self, job_id):
        from naukri_server.tools.tracking import _load_json
        async with self._lock:
            apps = _load_json(self._file_path)
        return next((a for a in apps if str(a.get("job_id")) == str(job_id)), None)

    async def save_application(self, app):
        from naukri_server.tools.tracking import _load_json, _save_json
        async with self._lock:
            apps = _load_json(self._file_path)
            existing = next((i for i, a in enumerate(apps) if str(a.get("job_id")) == str(app.get("job_id"))), None)
            if existing is not None:
                apps[existing] = app
            else:
                apps.append(app)
            _save_json(self._file_path, apps)

    async def delete_applications_before(self, date):
        from naukri_server.tools.tracking import _load_json, _save_json
        async with self._lock:
            apps = _load_json(self._file_path)
            before = len(apps)
            apps = [a for a in apps if a.get("applied_at", "") >= date]
            _save_json(self._file_path, apps)
            return before - len(apps)
