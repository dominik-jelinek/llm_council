import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.main import create_app
from tests.fixtures import CONFIGURATION, configure_test_app, parallel_reply, single_reply


class ProjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        configure_test_app(self.app)
        self.runs = self.app.state.runs
        self.store = self.app.state.store
        self.projects = self.app.state.projects
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        for target, reply in (
            ("query_models_parallel", parallel_reply),
            ("query_model", single_reply),
        ):
            mock = patch.object(council, target, AsyncMock(side_effect=reply))
            mock.start()
            self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await self.runs.close()
        await self.client.aclose()

    async def test_projects_are_durable_collapsed_groups_above_conversations(self):
        for name in (" Writing ", "Research <tag>"):
            response = await self.client.post(
                "/projects", data={"name": name}, headers={"HX-Request": "true"}
            )
            self.assertEqual(response.status_code, 200)
        restored = create_app(self.directory.name)
        projects = restored.state.projects.list()
        self.assertEqual([p["name"] for p in projects], ["Research <tag>", "Writing"])
        self.assertEqual(self.store.list(), [])
        page = await self.client.get("/")
        self.assertLess(
            page.text.index('id="projects-section"'),
            page.text.index('id="ungrouped-conversations"'),
        )
        self.assertIn("Research &lt;tag&gt;", page.text)
        for project in projects:
            tag = re.search(rf'<details id="project-{project["id"]}"[^>]*>', page.text)[0]
            self.assertNotIn(" open", tag)

    async def test_moving_changes_only_membership_and_preserves_branches_and_usage(self):
        project = self.projects.create("Research")
        first = self.runs.start(None, "Question")
        await first.task
        edited = self.runs.start(first.conversation_id, "Edited", edit_message_id=first.message_id)
        await edited.task
        before = self.store.get(first.conversation_id)
        response = await self.client.post(
            f"/conversations/{first.conversation_id}/project",
            data={"project_id": project["id"], "current_conversation_id": first.conversation_id},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Replace-Url", response.headers)
        saved = create_app(self.directory.name).state.store.get(first.conversation_id)
        self.assertEqual(saved.pop("project_id"), project["id"])
        self.assertEqual(saved, before)
        self.assertEqual(response.text.count(f'href="/conversations/{first.conversation_id}"'), 1)
        self.assertLess(
            response.text.index(f'href="/conversations/{first.conversation_id}"'),
            response.text.index('id="ungrouped-conversations"'),
        )
        response = await self.client.post(
            f"/conversations/{first.conversation_id}/project",
            data={"project_id": ""},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.store.get(first.conversation_id)["project_id"])
        self.assertEqual(len(self.runs.runs), 2)

    async def test_project_drafts_save_only_with_first_message_and_do_not_share_context(self):
        project = self.projects.create("Research")
        other = self.runs.start(None, "Private other question", project_id=project["id"])
        await other.task
        page = await self.client.get("/", params={"project": project["id"]})
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'data-draft-key="new:{project["id"]}"', page.text)
        self.assertIn(f'name="project_id" value="{project["id"]}"', page.text)
        self.assertEqual(len(self.store.list()), 1)
        response = await self.client.post(
            "/conversations/messages",
            data={"project_id": project["id"], "content": "Independent question"},
            headers={"HX-Request": "true"},
        )
        conversation_id = response.headers["HX-Push-Url"].rsplit("/", 1)[1]
        run = self.runs.active[conversation_id]
        self.assertEqual(run.history, [])
        self.assertEqual(run.configuration, CONFIGURATION)
        await run.task
        saved = (await self.client.get(f"/api/conversations/{conversation_id}")).json()
        self.assertEqual(saved["project_id"], project["id"])
        self.assertEqual(saved["messages"][0]["content"], "Independent question")
        self.assertEqual(len(saved["messages"]), 2)
        await self.runs.delete(conversation_id)
        self.assertIsNotNone(self.projects.get(project["id"]))
        self.assertIsNotNone(self.store.get(other.conversation_id))

    async def test_assignment_during_a_run_survives_subsequent_saves(self):
        started, release = asyncio.Event(), asyncio.Event()
        project = self.projects.create("Research")

        async def delayed(models, messages):
            started.set()
            await release.wait()
            return await parallel_reply(models, messages)

        with patch.object(council, "query_models_parallel", side_effect=delayed):
            run = self.runs.start(None, "Question")
            await asyncio.wait_for(started.wait(), 1)
            response = await self.client.post(
                f"/conversations/{run.conversation_id}/project",
                data={"project_id": project["id"]},
                headers={"HX-Request": "true"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(run.task.done())
            release.set()
            await run.task
        saved = self.store.get(run.conversation_id)
        self.assertEqual(saved["project_id"], project["id"])
        self.assertEqual(len(saved["messages"]), 2)
        self.assertTrue(saved["request_usage"])

    async def test_validation_and_cross_origin_requests_leave_data_unchanged(self):
        for name in ("  ", "x" * 81):
            response = await self.client.post(
                "/projects", data={"name": name}, headers={"HX-Request": "true"}
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.headers["HX-Retarget"], "#project-editor")
        self.assertEqual(self.projects.list(), [])
        response = await self.client.post(
            "/projects",
            data={"name": "Bad origin"},
            headers={"Origin": "https://unrelated.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.projects.list(), [])
        self.assertEqual((await self.client.get("/?project=missing")).status_code, 404)
        response = await self.client.post(
            "/conversations/messages", data={"content": "Question", "project_id": "missing"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.list(), [])
        conversation = self.store.create()
        response = await self.client.post(
            f"/conversations/{conversation['id']}/project",
            data={"project_id": "missing", "editor": "true"},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["HX-Retarget"], "#project-editor")
        self.assertEqual(self.store.get(conversation["id"]), conversation)

    async def test_failed_project_write_preserves_previous_projects(self):
        self.projects.create("Research")
        before = self.projects.list()
        with patch("backend.persistence.os.replace", side_effect=PermissionError("Read only")):
            response = await self.client.post(
                "/projects", data={"name": "Writing"}, headers={"HX-Request": "true"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["HX-Retarget"], "#project-editor")
        self.assertEqual(self.projects.list(), before)
        self.assertFalse(list(Path(self.directory.name).rglob("*.tmp")))

    async def test_native_project_forms_and_sidebar_updates_keep_project_draft_scope(self):
        self.assertIn("<!doctype html>", (await self.client.get("/projects/new")).text)
        response = await self.client.post("/projects", data={"name": "Research"})
        self.assertEqual(response.status_code, 303)
        project = self.projects.list()[0]
        conversation = self.store.create()
        url = f"/conversations/{conversation['id']}/project"
        self.assertIn("<!doctype html>", (await self.client.get(url)).text)
        response = await self.client.post(
            url, data={"project_id": project["id"], "current_project_id": project["id"]}
        )
        self.assertEqual(response.headers["Location"], f"/?project={project['id']}")
        response = await self.client.post(
            f"/conversations/{conversation['id']}/delete",
            data={"current_project_id": project["id"]},
            headers={"HX-Request": "true"},
        )
        self.assertIn(f'name="current_project_id" value="{project["id"]}"', response.text)
        self.assertNotIn('id="workspace"', response.text)
