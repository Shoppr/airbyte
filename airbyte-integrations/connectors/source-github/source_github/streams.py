#
# MIT License
#
# Copyright (c) 2020 Airbyte
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#


import tempfile
from abc import ABC
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional

import requests
import vcr
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.streams.http import HttpStream
from requests.exceptions import HTTPError

cache_file = tempfile.NamedTemporaryFile()


class GithubStream(HttpStream, ABC):
    url_base = "https://api.github.com/"

    primary_key = "id"

    # GitHub pagination could be from 1 to 100.
    page_size = 100

    # These fields will be used for data clearing. Put here keys which represent
    # objects `{}`, like `user`, `actor` etc.
    object_fields = ()

    # These fields will be used for data clearing. Put here keys which represent
    # lists `[]`, like `labels`, `assignees` etc.
    list_fields = ()

    def __init__(self, repository: str, **kwargs):
        super().__init__(**kwargs)
        self.repository = repository
        self._page = 1

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/{self.name}"

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        response_data = response.json()
        if response_data and len(response_data) == self.page_size:
            self._page += 1
            return {"page": self._page}

    def read_records(self, **kwargs) -> Iterable[Mapping[str, Any]]:
        try:
            yield from super().read_records(**kwargs)
        except HTTPError as e:
            error_msg = str(e)

            # This whole try/except situation in `read_records()` isn't good but right now in `self._send_request()`
            # function we have `response.raise_for_status()` so we don't have much choice on how to handle errors.
            # We added this try/except code because for private repositories `Teams` stream is not available and we get
            # "404 Client Error: Not Found for url: https://api.github.com/orgs/sherifnada/teams?per_page=100" error.
            if "/teams?" in error_msg:
                error_msg = f"Syncing Team stream isn't available for repository {self.repository}"

            self.logger.warn(error_msg)

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:

        params = {"per_page": self.page_size}

        if next_page_token:
            params.update(next_page_token)

        return params

    def request_headers(self, **kwargs) -> Mapping[str, Any]:
        # Without sending `User-Agent` header we will be getting `403 Client Error: Forbidden for url` error.
        return {
            "User-Agent": "PostmanRuntime/7.28.0",
        }

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        for record in response.json():  # GitHub puts records in an array.
            self.transform(record=record)
            yield record

    def transform(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Use this method to:
            - remove excessive fields from record;
            - minify subelements in the record. For example, if you have `reviews` record which looks like this:
            {
              "id": 671782869,
              "node_id": "MDE3OlB1bGxSZXF1ZXN0UmV2aWV3NjcxNzgyODY5",
              "user": {
                "login": "keu",
                "id": 1619536,
                ... <other fields>
              },
              "body": "lgtm, just  small comment",
              ... <other fields>
            }

            `user` subelement contains almost all possible fields fo user and it's not optimal to store such data in
            `reviews` record. We may leave only `user.id` field and save in to `user_id` field in the record. So if you
            need to do something similar with your record you may use this method.
        """
        for field in self.object_fields:
            field_value = record.pop(field, None)
            record[f"{field}_id"] = field_value.get("id") if field_value else None

        for field in self.list_fields:
            field_values = record.pop(field, [])
            record[field] = [value["id"] for value in field_values]


class SemiIncrementalGithubStream(GithubStream):
    cursor_field = "updated_at"

    # This flag is used to indicate that current stream supports `sort` and `direction` request parameters and that
    # we should break processing records if possible (see comment for `self._should_stop`).
    should_break_if_sorter = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # This flag is used for streams which support `sort` and `direction` request parameters
        # (`should_break_if_sorter` is set to `True`). If `sort` is set to `updated` and `direction` is set to `desc`
        # this means that latest records will be at the beginning of the response and after we processed those latest
        # records we can just stop and not process other record. This will increase speed of each incremental stream
        # which supports those 2 request parameters.
        self._should_stop = False

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]):
        """
        Return the latest state by comparing the cursor value in the latest record with the stream's most recent state
        object and returning an updated state object.
        """
        state_value = latest_cursor_value = latest_record.get(self.cursor_field)

        if current_stream_state.get(self.cursor_field):
            state_value = max(latest_cursor_value, current_stream_state[self.cursor_field])

        return {self.cursor_field: state_value}

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        if not self._should_stop:
            return super().next_page_token(response=response)

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        for record in response.json():  # GitHub puts records in an array.
            state_value = stream_state.get(self.cursor_field)

            if not state_value or record.get(self.cursor_field) > state_value:
                self.transform(record=record)
                yield record
            elif self.should_break_if_sorter and record.get(self.cursor_field) < state_value:
                self._should_stop = True
                break


class IncrementalGithubStream(SemiIncrementalGithubStream):
    def __init__(self, start_date, **kwargs):
        super().__init__(**kwargs)
        self._start_date = start_date

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:

        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, next_page_token=next_page_token)

        start_point = self._start_date
        if stream_state.get(self.cursor_field):
            start_point = max(start_point, stream_state[self.cursor_field])
        params["since"] = start_point

        return params


# Below are full refresh streams


class Assignees(GithubStream):
    pass


class Reviews(GithubStream):
    object_fields = ("user",)

    def path(
        self, stream_state: Mapping[str, Any] = None, stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> str:
        pull_request_number = stream_slice["pull_request_number"]
        return f"repos/{self.repository}/pulls/{pull_request_number}/reviews"

    def stream_slices(
        self, sync_mode: SyncMode, cursor_field: List[str] = None, stream_state: Mapping[str, Any] = None
    ) -> Iterable[Optional[Mapping[str, Any]]]:
        pull_requests_stream = PullRequests(authenticator=self.authenticator, repository=self.repository)
        for pull_request in pull_requests_stream.read_records(sync_mode=SyncMode.full_refresh):
            yield {"pull_request_number": pull_request["number"]}


class Collaborators(GithubStream):
    pass


class IssueLabels(GithubStream):
    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/labels"


class Teams(GithubStream):
    def path(self, **kwargs) -> str:
        owner, _ = self.repository.split("/")
        return f"orgs/{owner}/teams"


# Below are semi incremental streams


class Releases(SemiIncrementalGithubStream):
    cursor_field = "created_at"
    object_fields = ("author",)

    def transform(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        super().transform(record=record)

        assets = record.get("assets", [])
        for asset in assets:
            uploader = asset.pop("uploader", None)
            asset["uploader_id"] = uploader.get("id") if uploader else None


class Events(SemiIncrementalGithubStream):
    cursor_field = "created_at"
    object_fields = (
        "actor",
        "repo",
        "org",
    )


class PullRequests(SemiIncrementalGithubStream):
    should_break_if_sorter = True
    object_fields = (
        "user",
        "milestone",
        "assignee",
    )
    list_fields = (
        "labels",
        "assignees",
        "requested_reviewers",
        "requested_teams",
    )

    def read_records(self, **kwargs) -> Iterable[Mapping[str, Any]]:
        with vcr.use_cassette(cache_file.name, record_mode="new_episodes", serializer="json"):
            yield from super().read_records(**kwargs)

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/pulls"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(**kwargs)
        params["state"] = "all"
        params["sort"] = "updated"
        params["direction"] = "desc"
        return params

    def transform(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        super().transform(record=record)

        head = record.get("head", {})
        head_user = head.pop("user", None)
        head["user_id"] = head_user.get("id") if head_user else None
        head_repo = head.pop("repo", None)
        head["repo_id"] = head_repo.get("id") if head_repo else None

        base = record.get("base", {})
        base_user = base.pop("user", None)
        base["user_id"] = base_user.get("id") if base_user else None
        base_repo = base.pop("repo", None)
        base["repo_id"] = base_repo.get("id") if base_repo else None


class CommitComments(SemiIncrementalGithubStream):
    object_fields = ("user",)

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/comments"


class IssueMilestones(SemiIncrementalGithubStream):
    should_break_if_sorter = True
    object_fields = ("creator",)

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/milestones"

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(**kwargs)
        params["state"] = "all"
        params["sort"] = "updated"
        params["direction"] = "desc"
        return params


class Stargazers(SemiIncrementalGithubStream):
    primary_key = "user_id"
    cursor_field = "starred_at"
    object_fields = ("user",)

    def request_headers(self, **kwargs) -> Mapping[str, Any]:
        headers = super().request_headers(**kwargs)
        # We need to send below header if we want to get `starred_at` field. See docs (Alternative response with
        # star creation timestamps) - https://docs.github.com/en/rest/reference/activity#list-stargazers.
        headers["Accept"] = "application/vnd.github.v3.star+json"
        return headers


class Projects(SemiIncrementalGithubStream):
    object_fields = ("creator",)

    def request_params(self, **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(**kwargs)
        params["state"] = "all"
        return params

    def request_headers(self, **kwargs) -> Mapping[str, Any]:
        headers = super().request_headers(**kwargs)
        # Projects stream requires sending following `Accept` header. If we won't sent it
        # we'll get `415 Client Error: Unsupported Media Type` error.
        headers["Accept"] = "application/vnd.github.inertia-preview+json"
        return headers


class IssueEvents(SemiIncrementalGithubStream):
    cursor_field = "created_at"
    object_fields = (
        "actor",
        "issue",
    )

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/issues/events"


# Below are incremental streams


class Comments(IncrementalGithubStream):
    object_fields = ("user",)

    def path(self, **kwargs) -> str:
        return f"repos/{self.repository}/issues/comments"

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, next_page_token=next_page_token)
        params["sort"] = "updated"
        params["direction"] = "desc"

        return params


class Commits(IncrementalGithubStream):
    primary_key = "sha"
    cursor_field = "created_at"
    object_fields = (
        "author",
        "committer",
    )

    def transform(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        super().transform(record=record)

        # Record of the `commits` stream doesn't have an updated_at/created_at field at the top level (so we could
        # just write `record["updated_at"]` or `record["created_at"]`). Instead each record has such value in
        # `commit.author.date`. So the easiest way is to just enrich the record returned from API with top level
        # field `created_at` and use it as cursor_field.
        record["created_at"] = record["commit"]["author"]["date"]

    def parse_response(
        self,
        response: requests.Response,
        stream_state: Mapping[str, Any],
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None,
    ) -> Iterable[Mapping]:
        for record in response.json():  # GitHub puts records in an array.
            state_value = stream_state.get(self.cursor_field)

            self.transform(record=record)
            if not state_value or record.get(self.cursor_field) > state_value:
                yield record


class Issues(IncrementalGithubStream):
    object_fields = (
        "user",
        "assignee",
        "milestone",
    )
    list_fields = (
        "labels",
        "assignees",
    )

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, stream_slice=stream_slice, next_page_token=next_page_token)
        params["state"] = "all"
        params["sort"] = "updated"
        params["direction"] = "desc"

        return params
