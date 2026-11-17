"""Application contracts and GitHub REST adapter, independent of the runtime engine."""

import hashlib
import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(Data):
    number: int = Field(gt=0)
    title: str
    body: str
    state: Literal["open", "closed"]
    labels: list[str]
    revision: str
    url: str


class Snapshot(Data):
    repository: str
    available_labels: list[str]
    issues: list[Issue]


class ReadIssues(Data):
    pass


class AddLabels(Data):
    number: int = Field(gt=0)
    labels: list[str] = Field(min_length=1, max_length=10)
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class LabelResult(Data):
    number: int
    labels_present: list[str]
    observation: Literal["present_after_request", "already_present", "reconciled_present"]


class Recommendation(Data):
    number: int = Field(gt=0)
    source_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=2000)
    suggested_labels: list[str] = Field(max_length=10)
    missing_information: list[str] = Field(max_length=10)


class TriageReport(Data):
    items: list[Recommendation] = Field(max_length=10)


class GitHubError(Exception):
    def __init__(self, status: int):
        super().__init__(f"GitHub HTTP {status}")
        self.status = status


class SnapshotTooLarge(Exception):
    pass


class GitHub:
    """One repository, fixed API origin, no redirects and no implicit HTTP retries.

    Passing an httpx client is useful for MockTransport tests. The caller owns it.
    API credentials stay on this client, outside runtime state and model context.
    """

    def __init__(self, repository: str, client: httpx.AsyncClient):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected owner/repository")
        if any(part in (".", "..") for part in repository.split("/")):
            raise ValueError("Invalid repository")
        self.repository = repository
        self.client = client

    async def _request(self, method, path, **kwargs):
        response = await self.client.request(
            method,
            f"https://api.github.com/repos/{self.repository}/{path}",
            follow_redirects=False,
            **kwargs,
        )
        if not response.is_success:
            raise GitHubError(response.status_code)
        return response

    async def _pages(self, path, *, max_pages=10, **params):
        rows = []
        for page in range(1, max_pages + 1):
            response = await self._request(
                "GET", path, params={**params, "per_page": 100, "page": page}
            )
            batch = response.json()
            if not isinstance(batch, list):
                raise ValueError("Expected GitHub collection")
            rows.extend(batch)
            # Never follow a server-supplied URL with credentials; construct the next page.
            if "next" not in response.links:
                return rows
        raise SnapshotTooLarge("Collection exceeds the configured page limit")

    async def open_numbers(self, limit=5):
        rows = await self._pages("issues", state="open")
        return [row["number"] for row in rows if "pull_request" not in row][:limit]

    async def available_labels(self):
        rows = await self._pages("labels")
        return sorted(row["name"] for row in rows if not row.get("archived_at"))

    async def issue(self, number: int) -> Issue:
        if type(number) is not int or number < 1:
            raise ValueError("Issue number must be positive")
        raw = (await self._request("GET", f"issues/{number}")).json()
        if "pull_request" in raw:
            raise ValueError("Pull requests are outside this application's scope")
        content = {
            "repository": self.repository,
            "number": raw["number"],
            "title": raw["title"],
            "body": raw.get("body") or "",
            "state": raw["state"],
        }
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False)
        if len(encoded) > 20000:
            raise SnapshotTooLarge("Issue exceeds the supported content limit")
        revision = hashlib.sha256(encoded.encode()).hexdigest()
        return Issue(
            number=raw["number"],
            title=raw["title"],
            body=raw.get("body") or "",
            state=raw["state"],
            labels=sorted(label["name"] for label in raw["labels"]),
            revision=revision,
            url=f"https://github.com/{self.repository}/issues/{raw['number']}",
        )

    async def snapshot(self, numbers: tuple[int, ...]):
        if len(numbers) > 10 or len(set(numbers)) != len(numbers):
            raise ValueError("Select at most ten distinct issues")
        labels = await self.available_labels()
        return Snapshot(
            repository=self.repository,
            available_labels=labels,
            issues=[await self.issue(number) for number in numbers],
        )

    async def add_labels(self, number: int, labels: list[str]):
        # POST is additive. PUT would replace labels and risk deleting a maintainer's work.
        await self._request("POST", f"issues/{number}/labels", json={"labels": labels})
