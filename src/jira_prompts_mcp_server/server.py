import os
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from mcp.types import PromptMessage, TextContent
from pydantic import Field

from .jira_utils import JiraFetcher

LOGGER = logging.getLogger("jira_prompts")


class StrFallbackEncoder(json.JSONEncoder):
    """A custom JSON encoder to get around restrictions on unserializable objects

    A custom JSON encoder that falls back to an object's __str__ representation
    if the object is not directly serializable by the default JSON encoder.
    """

    def default(self, o):
        """
        Overrides the default method of JSONEncoder.

        If the object `obj` is not serializable by the standard encoder,
        this method is called. It returns the string representation (obj.__str__())
        of the object.

        Args:
            obj: The object to encode.

        Returns:
            A serializable representation of obj (its string form in this case).

        Raises:
            TypeError: If the default encoder itself encounters an issue after
                       this method returns (though unlikely if str() succeeds).
                       It primarily handles cases where the standard encoder fails.
        """
        try:
            # Let the base class default method try first (handles dates, etc.)
            # Although often the check happens *before* calling default,
            # this is more robust if the base class had more complex logic.
            # However, for this specific requirement (call str() on failure),
            # we can directly attempt the fallback.
            #
            # If json.JSONEncoder already raises TypeError for obj,
            # this 'default' method will be called.
            return str(o)
        except TypeError:
            # If str(obj) itself fails (less common), let the base class
            # raise the final TypeError.
            # This line is technically only reached if str(obj) itself fails,
            # which is rare. The primary path is just `return str(obj)`.
            return json.JSONEncoder.default(self, o)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[JiraFetcher]:
    """Initialize and clean up application resources."""
    jira_url = os.getenv("JIRA_URL")
    if jira_url is None:
        raise ValueError("JIRA_URL environment variable is not set")

    try:
        jira = JiraFetcher()

        # Log the startup information
        LOGGER.info("Starting Jira Prompts MCP server")
        os.system("notify-send 'Jira Prompts MCP server is starting'")

        jira_url = jira.config.url
        LOGGER.info(f"Jira URL: {jira_url}")

        # Provide context to the application
        yield jira
    finally:
        # Cleanup resources if needed
        pass


APP = FastMCP("jira-prompts-mcp", lifespan=server_lifespan)


def _postprocessing_for_issue_fields_(field_to_value):
    for name_field in ("status", "priority", "issuetype"):
        if name_field in field_to_value:
            field_to_value[name_field] = field_to_value[name_field].name
    for user_field in ("assignee", "reporter"):
        if user_field in field_to_value:
            if field_to_value[user_field] is None:
                field_to_value[user_field] = "N/A"
            else:
                field_to_value[user_field] = field_to_value[user_field].displayName
    if "parent" in field_to_value:
        field_to_value["parent"] = {
            "key": field_to_value["parent"].key,
            "summary": field_to_value["parent"].fields.summary,
            "status": field_to_value["parent"].fields.status.name,
        }


def get_issue_and_core_fields(jira_fetcher: JiraFetcher, arguments: dict[str, str] | None):
    if not arguments:
        raise ValueError("Argument `issue_key` is required")
    issue_key = arguments.get("issue_key", "")
    assert issue_key
    field_to_value, issue = jira_fetcher.get_issue_and_core_fields(issue_key)
    field_to_value["issue_key"] = issue_key
    _postprocessing_for_issue_fields_(field_to_value)
    return field_to_value, issue


@APP.prompt(
    name="jira-issue-brief",
)
def jira_issu_brief(issue_key: str = Field(description="The key/ID of the issue")):
    "Get the core information about a Jira issue, including its description, parent, status, type, priority, and assignee."
    ctx = get_context()
    # TODO: this is probably not best way to get the Jira fetcher instance
    jira_fetcher = ctx.request_context.lifespan_context
    field_to_value, issue = get_issue_and_core_fields(jira_fetcher, {"issue_key": issue_key})
    return PromptMessage(
        role="user", content=TextContent(type="text", text=json.dumps(field_to_value, cls=StrFallbackEncoder, indent=4))
    )


@APP.prompt(
    name="jira-issue-full",
)
def jira_issu_full(issue_key: str = Field(description="The key/ID of the issue")):
    "Get the full information about a Jira issue, including core information, linked issues, child tasks/sub tasks, and comments."
    ctx = get_context()
    # TODO: this is probably not best way to get the Jira fetcher instance
    jira_fetcher = ctx.request_context.lifespan_context
    field_to_value, issue = get_issue_and_core_fields(jira_fetcher, {"issue_key": issue_key})
    field_to_value["links"] = jira_fetcher.collect_links(issue)
    if field_to_value["issuetype"] != "Epic":
        field_to_value["subtasks"] = jira_fetcher.collect_subtasks(issue)
    else:
        field_to_value["child_tasks"] = jira_fetcher.collect_epic_children(issue)
    field_to_value["comments"] = jira_fetcher.collect_comments(issue)
    return PromptMessage(
        role="user", content=TextContent(type="text", text=json.dumps(field_to_value, cls=StrFallbackEncoder, indent=4))
    )
