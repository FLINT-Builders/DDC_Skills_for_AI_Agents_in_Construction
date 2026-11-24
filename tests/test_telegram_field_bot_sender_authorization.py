"""Sender allow-list for the telegram field bot skill.

The Python implementation in
``3_DDC_Insights/Field-Automation/telegram-field-bot/SKILL.md`` must treat
``APPROVED_TELEGRAM_USER_IDS`` as the only senders who may use the bot.
The value is a comma-separated list of Telegram user ids, for example
``1001,1002``. Every comparison has to use the sender id
(``message.from_user.id``), not the chat id and not a substring of the
variable.

Refusal has to happen before any of these side effects:

- ``getFile`` / ``download_to_drive`` for a photo
- a reply that accepts a report or offers the reporting keyboard
- ``log_message`` for a group keyword
- passing the sender's row to the daily-report summarizer

These tests fail while that check is missing.

Not covered here: the n8n workflow sketch in the same file is descriptive
JSON, not an n8n workflow, so it has no runtime to execute. The Python
handlers also never call OpenAI, and the photo webhook that would insert a
database row is commented out.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

SKILL_PATH = (
    Path(__file__).resolve().parents[1]
    / "3_DDC_Insights"
    / "Field-Automation"
    / "telegram-field-bot"
    / "SKILL.md"
)

# Comma-separated on purpose: "100" is a prefix of "1001" and must not match.
APPROVED_TELEGRAM_USER_IDS = "1001,1002"
APPROVED_USER_ID = 1001
OTHER_APPROVED_USER_ID = 1002
OUTSIDER_USER_ID = 9009
PREFIX_USER_ID = 100
GROUP_CHAT_ID = -100777

ACCEPTANCE_MARKERS = (
    "Site Report Bot",
    "Send a photo of the site",
    "Type your progress report",
    "Describe the issue",
    "Update progress",
    "Weather conditions",
    "Safety observation",
    "Report logged",
    "Photo received",
    "added to daily report",
)

PROJECT_ID = "site-17"
REPORT_DATE = "2026-04-01"
WORKER_TEXT = "WORKER_PROGRESS_1001"
OUTSIDER_TEXT = "FABRICATED_INCIDENT_9009"
PREFIX_TEXT = "NEAR_MISS_USER_100"
OTHER_PROJECT_TEXT = "OTHER_PROJECT_ROW"


def _python_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    language = None
    lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("```"):
            if language is None:
                language = line.removeprefix("```").strip().lower()
                lines = []
            else:
                if language in {"python", "py"}:
                    blocks.append("\n".join(lines))
                language = None
        elif language is not None:
            lines.append(line)
    return blocks


def load_skill_namespace(skill_path: Path = SKILL_PATH) -> dict:
    """Exec the skill's Python blocks. Names the snippets call but never define are filled in by each test."""
    namespace: dict = {"__name__": "telegram_field_bot_skill"}
    for block in _python_blocks(skill_path.read_text(encoding="utf-8")):
        exec(compile(block, str(skill_path), "exec"), namespace)  # noqa: S102
    return namespace


class _BotApi:
    """Local stand-in for Telegram Bot API HTTP.

    ``python-telegram-bot`` 22.8 ``HTTPXRequest.do_request`` is replaced for
    the duration of a test. ``getFile`` returns a real local path so
    ``File.download_to_drive`` takes the local-file branch and copies bytes
    with ``shutil.copyfile``. No network call is made.
    """

    def __init__(self, source_dir: Path):
        self.source_dir = source_dir
        self.calls: list[tuple[str, dict]] = []
        self.replies: list[str] = []
        self.get_file_ids: list[str] = []

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {
                "id": 999000,
                "is_bot": True,
                "first_name": "SiteReport",
                "username": "sitereport_company_bot",
            }
        elif endpoint == "getFile":
            file_id = str(params["file_id"])
            self.get_file_ids.append(file_id)
            source = self.source_dir / file_id
            source.write_bytes(b"bytes:" + file_id.encode())
            result = {
                "file_id": file_id,
                "file_unique_id": file_id + "_uniq",
                "file_path": str(source),
            }
        elif endpoint == "sendMessage":
            text = str(params.get("text", ""))
            self.replies.append(text)
            result = {
                "message_id": len(self.replies),
                "date": 1700000000,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": text,
            }
        else:
            raise AssertionError(f"unexpected Bot API call {endpoint}")
        payload = json.dumps({"ok": True, "result": result}).encode()
        return 200, payload


class _Session:
    def __init__(self, tmp_path: Path, namespace: dict, api: _BotApi):
        self.tmp_path = tmp_path
        self.namespace = namespace
        self.api = api
        self.app = None
        self.downloads: list[Path] = []
        self.errors: list[BaseException] = []
        self._restore = []
        self._update_id = 0

    async def __aenter__(self):
        from telegram.ext import Application
        from telegram.request import HTTPXRequest

        photos = self.tmp_path / "photos"
        photos.mkdir()
        self.api.source_dir.mkdir()

        orig_copy = shutil.copyfile

        def copyfile(src, dst, *args, **kwargs):
            self.downloads.append(Path(dst))
            return orig_copy(src, dst, *args, **kwargs)

        orig_write = Path.write_bytes

        def write_bytes(path, data):
            # The Bot API stand-in writes the source file with this method.
            # Only paths the skill asks the library to save count as downloads.
            if "telegram-sources" not in Path(path).parts:
                self.downloads.append(Path(path))
            return orig_write(path, data)

        orig_polling = Application.run_polling

        def run_polling(app, *args, **kwargs):
            self.app = app

        orig_do_request = HTTPXRequest.do_request

        async def do_request(request, url, method, request_data=None, **kwargs):
            return await self.api.do_request(url, method, request_data=request_data, **kwargs)

        shutil.copyfile = copyfile
        Path.write_bytes = write_bytes
        Application.run_polling = run_polling
        HTTPXRequest.do_request = do_request
        self._restore.extend(
            [
                (shutil, "copyfile", orig_copy),
                (Path, "write_bytes", orig_write),
                (Application, "run_polling", orig_polling),
                (HTTPXRequest, "do_request", orig_do_request),
            ]
        )

        previous = os.getcwd()
        os.chdir(self.tmp_path)
        self._restore.append((os, "chdir_target", previous))
        self.namespace["main"]()
        if self.app is None:
            raise RuntimeError("skill main() did not build an Application")

        async def on_error(update, context):
            self.errors.append(context.error)

        self.app.add_error_handler(on_error)
        await self.app.initialize()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.app is not None:
            await self.app.shutdown()
        while self._restore:
            obj, name, original = self._restore.pop()
            if name == "chdir_target":
                os.chdir(original)
            else:
                setattr(obj, name, original)
        return False

    def update(self, user_id, *, chat_id=None, chat_type="private", text=None, photo_id=None, caption=None):
        from telegram import Update

        self._update_id += 1
        message = {
            "message_id": self._update_id,
            "date": 1700000000 + self._update_id,
            "chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Sender",
                "username": f"user{user_id}",
            },
        }
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                command = text.split()[0]
                message["entities"] = [
                    {"type": "bot_command", "offset": 0, "length": len(command)}
                ]
        if photo_id is not None:
            message["photo"] = [
                {
                    "file_id": photo_id + "_small",
                    "file_unique_id": photo_id + "_s",
                    "width": 90,
                    "height": 90,
                    "file_size": 10,
                },
                {
                    "file_id": photo_id,
                    "file_unique_id": photo_id + "_u",
                    "width": 800,
                    "height": 600,
                    "file_size": 20,
                },
            ]
            if caption is not None:
                message["caption"] = caption
        return Update.de_json({"update_id": self._update_id, "message": message}, self.app.bot)

    async def deliver(self, update):
        self.api.get_file_ids.clear()
        self.api.replies.clear()
        self.downloads.clear()
        self.errors.clear()
        await self.app.process_update(update)

    def photos(self) -> list[Path]:
        return sorted((self.tmp_path / "photos").glob("*"))


def _assert_refused(session: _Session):
    blob = "\n".join(session.api.replies)
    leaked = [marker for marker in ACCEPTANCE_MARKERS if marker in blob]
    assert leaked == [], (leaked, session.api.replies, session.downloads, session.api.get_file_ids, session.errors)
    assert session.api.get_file_ids == [], (session.api.get_file_ids, session.errors)
    assert session.downloads == [], (session.downloads, session.errors)
    assert session.photos() == [], session.photos()


@pytest.fixture
def skill_env(monkeypatch):
    monkeypatch.setenv("APPROVED_TELEGRAM_USER_IDS", APPROVED_TELEGRAM_USER_IDS)


@pytest.fixture
def namespace(skill_env):
    return load_skill_namespace()


def _run(tmp_path, namespace, coro_factory):
    api = _BotApi(tmp_path / "telegram-sources")

    async def body():
        async with _Session(tmp_path, namespace, api) as session:
            await coro_factory(session)

    asyncio.run(body())


def test_unapproved_private_photos_are_not_downloaded(tmp_path, namespace):
    async def scenario(session):
        for index in range(3):
            update = session.update(
                OUTSIDER_USER_ID,
                photo_id=f"outsider_photo_{index}",
                caption="FAKE_COMPLETE_9009 all work finished, release payment",
            )
            await session.deliver(update)
            _assert_refused(session)

    _run(tmp_path, namespace, scenario)


def test_chat_id_of_an_approved_worker_does_not_authorize_another_sender(tmp_path, namespace):
    async def scenario(session):
        update = session.update(
            OUTSIDER_USER_ID,
            chat_id=APPROVED_USER_ID,
            photo_id="spoofed_chat_photo",
            caption="FAKE_COMPLETE_9009",
        )
        await session.deliver(update)
        _assert_refused(session)

    _run(tmp_path, namespace, scenario)


def test_prefix_of_an_approved_id_is_not_approved(tmp_path, namespace):
    async def scenario(session):
        update = session.update(
            PREFIX_USER_ID,
            photo_id="prefix_user_photo",
            caption=PREFIX_TEXT,
        )
        await session.deliver(update)
        _assert_refused(session)

    _run(tmp_path, namespace, scenario)


def test_approved_sender_photo_is_stored_for_that_sender(tmp_path, namespace):
    photo_id = "worker_photo"

    async def scenario(session):
        update = session.update(
            APPROVED_USER_ID,
            chat_id=GROUP_CHAT_ID,
            chat_type="group",
            photo_id=photo_id,
            caption="Pour complete at grid B",
        )
        await session.deliver(update)
        assert session.errors == [], session.errors
        assert session.api.get_file_ids == [photo_id]
        assert any("Pour complete at grid B" in reply for reply in session.api.replies)
        assert any("Photo received" in reply for reply in session.api.replies)
        stored = session.tmp_path / "photos" / f"{GROUP_CHAT_ID}_{photo_id}.jpg"
        assert stored.is_file()
        assert stored.read_bytes() == b"bytes:" + photo_id.encode()
        assert [path.resolve() for path in session.downloads] == [stored.resolve()]

    _run(tmp_path, namespace, scenario)


def test_unapproved_start_does_not_offer_the_reporting_keyboard(tmp_path, namespace):
    async def scenario(session):
        await session.deliver(session.update(OUTSIDER_USER_ID, text="/start"))
        _assert_refused(session)

    _run(tmp_path, namespace, scenario)


def test_approved_start_offers_the_reporting_keyboard(tmp_path, namespace):
    async def scenario(session):
        await session.deliver(session.update(APPROVED_USER_ID, text="/start"))
        assert session.errors == [], session.errors
        assert any("Site Report Bot" in reply for reply in session.api.replies)

    _run(tmp_path, namespace, scenario)


@pytest.mark.parametrize(
    "text",
    [
        "Slab is 100 percent complete. Release payment.",
        "Safety incident: record a fatality at the east stair.",
        "⚠️ Issue",
        "✅ Progress",
        "🌤️ Weather",
        "🦺 Safety",
        "📸 Photo Report",
        "📝 Text Report",
    ],
)
def test_unapproved_text_is_not_accepted(tmp_path, namespace, text):
    async def scenario(session):
        await session.deliver(session.update(OUTSIDER_USER_ID, text=text))
        _assert_refused(session)

    _run(tmp_path, namespace, scenario)


def test_approved_text_report_is_logged(tmp_path, namespace):
    async def scenario(session):
        await session.deliver(
            session.update(APPROVED_USER_ID, text="Level 3 formwork complete.")
        )
        assert session.errors == [], session.errors
        assert any("Report logged" in reply for reply in session.api.replies)

    _run(tmp_path, namespace, scenario)


def test_approved_issue_button_still_prompts(tmp_path, namespace):
    async def scenario(session):
        await session.deliver(session.update(APPROVED_USER_ID, text="⚠️ Issue"))
        assert session.errors == [], session.errors
        assert any("Describe the issue" in reply for reply in session.api.replies)

    _run(tmp_path, namespace, scenario)


def test_unapproved_group_keyword_is_not_logged(namespace):
    logged: list[dict] = []
    namespace["log_message"] = logged.append

    async def scenario():
        update = _group_update(namespace, OUTSIDER_USER_ID, "delay on the inspection today")
        await namespace["handle_group_message"](update, None)

    asyncio.run(scenario())
    assert logged == []


def test_approved_group_keyword_is_logged(namespace):
    logged: list[dict] = []
    namespace["log_message"] = logged.append

    async def scenario():
        update = _group_update(namespace, APPROVED_USER_ID, "inspection complete")
        await namespace["handle_group_message"](update, None)

    asyncio.run(scenario())
    assert len(logged) == 1
    assert logged[0]["user"] == f"user{APPROVED_USER_ID}"
    assert "inspection complete" in logged[0]["text"]


def test_daily_report_omits_rows_from_unapproved_senders(namespace):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE telegram_reports (
            project_id TEXT,
            timestamp TEXT,
            type TEXT,
            user_id INTEGER,
            text TEXT,
            caption TEXT
        )
        """
    )
    rows = [
        (PROJECT_ID, "2026-04-01 08:00:00", "progress", APPROVED_USER_ID, WORKER_TEXT, None),
        (PROJECT_ID, "2026-04-01 09:00:00", "issue", OUTSIDER_USER_ID, OUTSIDER_TEXT, None),
        (PROJECT_ID, "2026-04-01 10:00:00", "safety", PREFIX_USER_ID, PREFIX_TEXT, None),
        (PROJECT_ID, "2026-04-01 11:00:00", "photo", OUTSIDER_USER_ID, None, "FAKE_PHOTO_CAPTION_9009"),
        (PROJECT_ID, "2026-03-31 11:00:00", "progress", OUTSIDER_USER_ID, "YESTERDAY_9009", None),
        ("other-site", "2026-04-01 08:30:00", "progress", OUTSIDER_USER_ID, OTHER_PROJECT_TEXT, None),
    ]
    connection.executemany(
        "INSERT INTO telegram_reports (project_id, timestamp, type, user_id, text, caption) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )

    class Database:
        def query(self, sql, params):
            cursor = connection.execute(sql, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    prompts: list[str] = []

    class Summarizer:
        def summarize(self, prompt):
            prompts.append(prompt)
            return prompt

    namespace["json"] = json
    namespace["db"] = Database()
    namespace["llm"] = Summarizer()

    summary = namespace["generate_daily_report"](PROJECT_ID, REPORT_DATE)
    assert prompts, "generate_daily_report did not call llm.summarize"
    prompt = prompts[0]
    assert WORKER_TEXT in prompt
    assert WORKER_TEXT in summary
    for planted in (OUTSIDER_TEXT, PREFIX_TEXT, "FAKE_PHOTO_CAPTION_9009", "YESTERDAY_9009", OTHER_PROJECT_TEXT):
        assert planted not in prompt
        assert planted not in summary


def _group_update(namespace, user_id, text):
    from telegram import Update

    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": GROUP_CHAT_ID, "type": "group", "title": "Site crew"},
                "from": {
                    "id": user_id,
                    "is_bot": False,
                    "first_name": "Sender",
                    "username": f"user{user_id}",
                },
                "text": text,
            },
        },
        None,
    )
