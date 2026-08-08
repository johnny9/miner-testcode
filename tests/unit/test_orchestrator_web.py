from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from miner_testcode.orchestrator.config import ConfigStore
from miner_testcode.orchestrator.database import OrchestratorDatabase
from test_orchestrator import configuration

try:
    from httpx import ASGITransport, AsyncClient

    from miner_testcode.orchestrator.web import create_app
except ImportError:
    ASGITransport = None  # type: ignore[assignment,misc]
    AsyncClient = None  # type: ignore[assignment,misc]
    create_app = None  # type: ignore[assignment]


@unittest.skipUnless(AsyncClient is not None, "orchestrator web extra is not installed")
class OrchestratorApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "orchestrator.yaml"
        self.path.write_text(
            yaml.safe_dump(configuration(self.root), sort_keys=False), encoding="utf-8"
        )
        self.store = ConfigStore(self.path)
        self.database = OrchestratorDatabase(self.root / "state.sqlite3")
        self.addCleanup(self.database.close)
        with mock.patch.dict(
            "os.environ", {"MINER_ORCHESTRATOR_API_TOKEN": "local-token"}
        ):
            app = create_app(self.store, self.database)
            self.client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://orchestrator.test"
            )
        self.addAsyncCleanup(self.client.aclose)

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer local-token"}

    async def test_exposes_openapi_health_and_configuration_etag(self) -> None:
        health = await self.client.get("/api/v1/health")
        config = await self.client.get("/api/v1/config")
        schema = await self.client.get("/openapi.json")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.headers["etag"], self.store.snapshot.etag)
        self.assertIn("/api/v1/gates/{resource_id}", schema.json()["paths"])
        self.assertIn("/api/v1/lab/devices/{device_id}/photo", schema.json()["paths"])

    async def test_resource_mutation_requires_token_and_current_etag(self) -> None:
        current = await self.client.get("/api/v1/config")
        repository = self.store.snapshot.document["repositories"]["firmware"] | {
            "polling_seconds": 120
        }
        denied = await self.client.patch(
            "/api/v1/repositories/firmware",
            headers={"If-Match": current.headers["etag"]},
            json=repository,
        )
        changed = await self.client.patch(
            "/api/v1/repositories/firmware",
            headers={**self.auth, "If-Match": current.headers["etag"]},
            json=repository,
        )
        stale = await self.client.patch(
            "/api/v1/repositories/firmware",
            headers={**self.auth, "If-Match": current.headers["etag"]},
            json=repository,
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(
            ConfigStore(self.path).snapshot.document["repositories"]["firmware"][
                "polling_seconds"
            ],
            120,
        )

    async def test_yaml_editor_and_photo_api_use_the_same_configuration_store(self) -> None:
        dashboard = await self.client.get("/")
        current = await self.client.get("/api/v1/config")
        photo = await self.client.put(
            "/api/v1/lab/devices/bonanza/photo",
            headers={
                **self.auth,
                "If-Match": current.headers["etag"],
                "Content-Type": "image/png",
            },
            content=b"\x89PNG\r\n\x1a\nexample",
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn("cdn.jsdelivr.net", dashboard.text)
        self.assertEqual(photo.status_code, 200)
        configured_photo = self.store.snapshot.document["lab"]["devices"]["bonanza"][
            "photo"
        ]
        self.assertTrue((self.path.parent / configured_photo).exists())


if __name__ == "__main__":
    unittest.main()
