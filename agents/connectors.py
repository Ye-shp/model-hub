"""The owner's own MCP servers and HTTP APIs, as tools for Cowork.

Set up from an owner Cowork chat (the chat site passes the text after the command here, unseen by the model):

    /connect mcp <name> <https url> [bearer=TOKEN] [header="Name: value"]...      remote MCP (streamable HTTP; …/sse URLs use SSE)
    /connect mcp <name> stdio <command> [args…] [env:KEY=value]...                 local MCP, run as the owner's sandbox user
    /connect api <name> <base url> [bearer=TOKEN] [header="Name: value"]... [query:key=value]... [methods=GET,POST] [about="…"]
    /connect mcp <name> off  ·  /connect api <name> off

Entries live root-only in DATA/connectors.json. Only the owner's tasks get them. Small MCP catalogs are offered as
<server>__<tool>. Large catalogs stay in the controller and expose compact discovery/invocation tools, so a connected
media service does not fill every ordinary chat's prompt with schemas. APIs go through one call_api tool that adds the
stored headers and query values itself, so keys never reach the model, and only calls paths under its configured base URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
from contextlib import AsyncExitStack
from urllib.parse import parse_qsl, urlsplit

try:
    import httpx2 as httpx
except ImportError:
    import httpx

import store

NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_OUTPUT = 20000
CONNECT_SECONDS = 90
CALL_SECONDS = 180
# Preserve the direct tool interface for small servers; cap all directly advertised
# MCP catalogs in one job together. Large schemas remain available through paging.
DIRECT_TOOL_LIMIT = 12
DIRECT_CATALOG_CHARACTERS = 16000
DISCOVERY_RESULTS = 8
SCHEMA_PAGE_CHARACTERS = 6000
DISCOVERY_OUTPUT_CHARACTERS = 12000
MAX_MCP_TOOL_NAME = 256
HIGGSFIELD_GUIDE = (
    "Higgsfield uses the owner's connected account. Use the discovered Higgsfield tools and their schemas; "
    "do not invent model or preset IDs. Inspect the model/preset and quote its cost before a requested generation. "
    "Looking up models, balance, or status does not authorize generation. Follow any returned choice that needs "
    "the user's answer. Save returned job IDs in the workspace before waiting, and resume those IDs after an "
    "interruption; never blindly repeat a submission whose outcome is unknown. Report success only when the "
    "job is complete, and keep its result URLs. Stopping a local wait does not cancel a remote generation. "
    "Provider tools cannot read this Hub's local paths; use a supported upload flow for references and do not "
    "claim files were uploaded without a successful upload and confirmation. No Higgsfield login credentials "
    "are needed in your tool arguments. Generating media does not authorize publishing or messaging it."
)


# ---------------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------------
def _path():
    return store.DATA / "connectors.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        data = {}
    return {"mcp": data.get("mcp") or {}, "api": data.get("api") or {}}


def _save(data: dict) -> None:
    store.DATA.mkdir(parents=True, exist_ok=True)
    temporary = _path().with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(temporary, _path())


def summary() -> dict:
    """What's connected, without secrets."""
    data = load()
    mcp = {name: {"transport": e["transport"],
                  "target": urlsplit(e["url"]).netloc if e.get("url") else os.path.basename(e["command"][0])}
           for name, e in data["mcp"].items()}
    api = {name: {"host": urlsplit(e["base_url"]).netloc, "methods": e["methods"], "about": e.get("about", "")}
           for name, e in data["api"].items()}
    return {"mcp": mcp, "api": api}


# ---------------------------------------------------------------------------------------------
# /connect mcp … and /connect api …
# ---------------------------------------------------------------------------------------------
def _header(value: str) -> tuple[str, str]:
    name, sep, content = value.partition(":")
    name, content = name.strip(), content.strip()
    if not sep or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) or not content or len(content) > 4000 or "\n" in content:
        raise ValueError('Write headers as header="Name: value"')
    return name, content


def _url(value: str, what: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"The {what} must be an http(s) URL without a username or password in it")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"Use https for the {what} (plain http only for servers on this box)")
    return value.rstrip("/") if what == "API base URL" else value


def configure(kind: str, text: str) -> dict:
    """Apply the words after '/connect mcp' or '/connect api'. Returns {'name', 'removed'|'entry summary'}."""
    if kind not in {"mcp", "api"}:
        raise ValueError("Use /connect mcp … or /connect api …")
    try:
        words = shlex.split(text or "")
    except ValueError:
        raise ValueError("Unbalanced quotes in the command") from None
    usage = (__doc__.split("\n\n")[1]).strip()
    if len(words) < 2:
        raise ValueError("Usage:\n" + usage)
    name = words[0].lower()
    if not NAME.fullmatch(name):
        raise ValueError("Names are 1-24 characters: lowercase letters, digits, - and _, starting with a letter")
    data = load()
    if words[1].lower() == "off":
        removed = data[kind].pop(name, None) is not None
        _save(data)
        return {"name": name, "kind": kind, "removed": removed}
    headers, query, env, rest, extra = {}, {}, {}, [], {}
    for word in words[1:]:
        key, sep, value = word.partition("=")
        if word.startswith("env:") and "=" in word:
            env_key, _, env_value = word[4:].partition("=")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", env_key):
                raise ValueError(f"Bad environment variable name: {env_key}")
            env[env_key] = env_value
        elif word.startswith("query:") and "=" in word:
            query_key, _, query_value = word[6:].partition("=")
            query[query_key] = query_value
        elif sep and key in {"bearer", "header", "methods", "about"}:
            if key == "bearer":
                headers["Authorization"] = f"Bearer {value.strip()}"
            elif key == "header":
                header_name, header_value = _header(value)
                headers[header_name] = header_value
            else:
                extra[key] = value.strip()
        else:
            rest.append(word)
    if kind == "mcp":
        if rest and rest[0].lower() == "stdio":
            if len(rest) < 2:
                raise ValueError("Give the command after stdio, e.g. stdio npx -y @modelcontextprotocol/server-memory")
            entry = {"transport": "stdio", "command": rest[1:], "env": env}
        else:
            if len(rest) != 1:
                raise ValueError("Give one server URL (or stdio and a command)")
            url = _url(rest[0], "MCP server URL")
            entry = {"transport": "sse" if urlsplit(url).path.rstrip("/").endswith("/sse") else "http", "url": url,
                     "headers": headers}
    else:
        if len(rest) != 1:
            raise ValueError("Give one base URL for the API")
        methods = [m.strip().upper() for m in extra.get("methods", "GET,POST").split(",") if m.strip()]
        if not methods or any(m not in METHODS for m in methods):
            raise ValueError("methods= takes a comma list of " + ", ".join(METHODS))
        entry = {"base_url": _url(rest[0], "API base URL"), "headers": headers, "query": query, "methods": methods,
                 "about": extra.get("about", "")[:300]}
    entry["saved_at"] = store.now()
    data[kind][name] = entry
    _save(data)
    return {"name": name, "kind": kind, "removed": False, **summary()[kind][name]}


# ---------------------------------------------------------------------------------------------
# MCP servers -> tools
# ---------------------------------------------------------------------------------------------
def _server(name: str, entry: dict, space):
    from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
    options = {"name": name, "cache_tools_list": True, "client_session_timeout_seconds": CALL_SECONDS}
    if entry["transport"] == "stdio":
        argv = space.argv(list(entry["command"]), cpu_seconds=4 * 3600)  # the sandbox user, never root
        return MCPServerStdio({"command": argv[0], "args": argv[1:], "env": space.env(entry.get("env") or {}),
                               "cwd": str(space.dir)}, **options)
    params = {"url": entry["url"], "headers": entry.get("headers") or {}, "timeout": 30}
    if entry.get("auth") == "higgsfield":
        import higgsfield
        if entry["transport"] != "http" or entry["url"] != higgsfield.MCP_URL:
            raise ValueError("Higgsfield authentication is restricted to its official MCP endpoint")
        params["auth"] = higgsfield.auth()
    return (MCPServerSse if entry["transport"] == "sse" else MCPServerStreamableHttp)(params, **options)


def tool_name(server: str, tool: str) -> str:
    return (server.replace("-", "_") + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", tool))[:64]


def catalog_tool_name(server: str, operation: str) -> str:
    """Separate normalized server names and reserve room for the operation."""
    digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:32]
    return f"mcp_{digest}__{operation}"


def _field(item, *names):
    """MCP SDK 1.x uses camelCase fields (inputSchema, isError), 2.x snake_case: accept both."""
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def render(result, media_results: bool = False) -> str:
    """An MCP CallToolResult as text for the model."""
    parts = []
    structured = _field(result, "structured_content", "structuredContent")
    # Higgsfield's structured results contain job IDs and URLs; a widget-only
    # text block must not hide those from a client that has no provider widget.
    if media_results and structured:
        parts.append(json.dumps(structured, ensure_ascii=False))
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(item.text)
        elif kind == "resource" and getattr(getattr(item, "resource", None), "text", None):
            parts.append(item.resource.text)
        elif media_results and kind == "resource_link":
            parts.append(f"{getattr(item, 'name', 'Result')}: {getattr(item, 'uri', '')}")
        else:
            parts.append(f"[{kind or 'content'} omitted]")
    if structured and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False))
    text = "\n".join(parts) or "(no output)"
    if _field(result, "is_error", "isError"):
        text = "The tool reported an error: " + text
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n[… {len(text) - MAX_OUTPUT} more characters cut]"


def wrap(server_name: str, server, tool, still_running, log, higgsfield: bool = False):
    """One MCP tool as a function tool named <server>__<tool>."""
    from agents import FunctionTool
    schema = dict(_field(tool, "input_schema", "inputSchema") or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})

    async def invoke(context, arguments: str) -> str:
        still_running()
        try:
            values = json.loads(arguments or "{}")
        except ValueError:
            return "The arguments weren't valid JSON."
        log("tool", f"{server_name}: {tool.name}")
        try:
            async with asyncio.timeout(CALL_SECONDS):
                result = await server.call_tool(tool.name, values if isinstance(values, dict) else {})
        except TimeoutError:
            if higgsfield:
                return (f"Higgsfield {tool.name} timed out. A submitted generation may still be running; "
                        "check its existing job ID before submitting again.")
            return f"{server_name}.{tool.name} took longer than {CALL_SECONDS} s and was abandoned."
        except Exception as error:
            if higgsfield:
                return (f"Higgsfield {tool.name} failed ({type(error).__name__}). Check the existing job status; "
                        "if sign-in expired, reconnect with /connect higgsfield.")
            return f"{server_name}.{tool.name} failed: {type(error).__name__}: {str(error)[:400]}"
        return render(result, media_results=higgsfield)

    description = (tool.description or tool.name).strip()[:900]
    return FunctionTool(name=tool_name(server_name, tool.name), description=f"[{server_name} MCP] {description}",
                        params_json_schema=schema, on_invoke_tool=invoke, strict_json_schema=False)


def discovery_tools(server_name: str, wrapped: list, remote: list, still_running) -> list:
    """Keep a job's full MCP catalog private; discover and call exact upstream names.

    Discovery is not an authorization gate: an exact name remembered from an earlier
    task may still be called if it is in this task's current catalog. Unknown names
    and ambiguous duplicate names never reach the remote server. Schema pages are
    JSON string fragments with offsets, not shortened or invalid schema objects.
    """
    from agents import FunctionTool

    catalog = {}
    ambiguous = set()
    for source, tool in zip(remote, wrapped):
        if not isinstance(source.name, str) or not source.name or len(source.name) > MAX_MCP_TOOL_NAME:
            continue
        if source.name in catalog:
            ambiguous.add(source.name)
        else:
            catalog[source.name] = tool
    for name in ambiguous:
        del catalog[name]

    async def discover(context, arguments: str) -> str:
        still_running()
        try:
            values = json.loads(arguments or "{}")
            if not isinstance(values, dict):
                raise ValueError()
            query, name = values.get("query", ""), values.get("name", "")
            offset, limit = values.get("offset", 0), values.get("limit", 5)
            if (not isinstance(query, str) or not isinstance(name, str)
                    or type(offset) is not int or offset < 0 or type(limit) is not int):
                raise ValueError()
        except (ValueError, TypeError):
            return "Discovery needs query/name strings, a nonnegative integer offset, and an integer limit."
        if name:
            tool = catalog.get(name)
            if tool is None:
                return "Unknown or ambiguous tool name. Search this server's current catalog first."
            schema = json.dumps(tool.params_json_schema, ensure_ascii=False, separators=(",", ":"))
            if offset > len(schema):
                return "Schema offset is outside the schema. Use the previous page's next_offset."
            end = min(len(schema), offset + SCHEMA_PAGE_CHARACTERS)
            def page(stop):
                return json.dumps({"name": name, "description": tool.description, "schema_json": schema[offset:stop],
                                   "offset": offset, "total_characters": len(schema),
                                   "next_offset": stop if stop < len(schema) else None}, ensure_ascii=False)
            # Schema strings contain quotes/backslashes that are escaped again in
            # this outer JSON. Bound the actual serialized output, not just text.
            output = page(end)
            if len(output) > DISCOVERY_OUTPUT_CHARACTERS:
                low, high = offset, end
                while low < high:
                    middle = (low + high + 1) // 2
                    if len(page(middle)) <= DISCOVERY_OUTPUT_CHARACTERS:
                        low = middle
                    else:
                        high = middle - 1
                output = page(low)
            return output
        else:
            words = re.findall(r"[\w]+", query.casefold())
            matches = []
            for tool_name_, tool in catalog.items():
                haystack = (tool_name_ + " " + tool.description).casefold()
                score = sum(word in haystack for word in words)
                if not words or score:
                    matches.append((score, tool_name_, tool))
            matches.sort(key=lambda match: (-match[0], match[1]))
            limit = max(1, min(limit, DISCOVERY_RESULTS))
            selected = matches[offset:offset + limit]
            while True:
                end = offset + len(selected)
                result = {"tools": [{"name": name_, "description": tool.description[:240]}
                                    for _, name_, tool in selected],
                          "total_matches": len(matches), "next_offset": end if end < len(matches) else None,
                          "schema_help": "Request an exact name to read its complete schema_json in pages; join pages in offset order."}
                output = json.dumps(result, ensure_ascii=False)
                if len(output) <= DISCOVERY_OUTPUT_CHARACTERS or not selected:
                    return output
                selected.pop()

    async def invoke(context, arguments: str) -> str:
        still_running()
        try:
            values = json.loads(arguments or "{}")
            if (not isinstance(values, dict) or not isinstance(values.get("name"), str)
                    or not isinstance(values.get("arguments"), dict)):
                raise ValueError()
        except (ValueError, TypeError):
            return "Invocation needs an exact tool name and an arguments JSON object."
        tool = catalog.get(values["name"])
        if tool is None:
            return "Unknown or ambiguous tool name. Search this server's current catalog first."
        # Reuse the existing callback, including cancellation checks, logging,
        # call timeout, Higgsfield warnings and structured media result handling.
        return await tool.on_invoke_tool(context, json.dumps(values["arguments"], ensure_ascii=False))

    search_schema = {"type": "object", "properties": {
        "query": {"type": "string", "description": "Words describing the capability to find; empty lists tools."},
        "name": {"type": "string", "description": "Exact discovered upstream name to read its schema; empty searches."},
        "offset": {"type": "integer", "minimum": 0,
                   "description": "Result index for search, or character offset for schema_json paging."},
        "limit": {"type": "integer", "minimum": 1, "maximum": DISCOVERY_RESULTS}}, "additionalProperties": False}
    call_schema = {"type": "object", "properties": {"name": {"type": "string"},
                   "arguments": {"type": "object", "additionalProperties": True}},
                   "required": ["name", "arguments"], "additionalProperties": False}
    return [FunctionTool(name=catalog_tool_name(server_name, "discover_tools"),
                description=f"[{server_name} MCP] Find relevant connected tools. Request an exact name for its complete "
                            "schema_json; follow next_offset and join pages. Discovery is read-only.",
                params_json_schema=search_schema, on_invoke_tool=discover, strict_json_schema=False),
            FunctionTool(name=catalog_tool_name(server_name, "invoke_tool"),
                description=f"[{server_name} MCP] Call an exact current catalog tool name with its arguments object. "
                            "Discover its schema first unless already known. Follow the user's authorization and "
                            "the provider's requirements before submitting work.",
                params_json_schema=call_schema, on_invoke_tool=invoke, strict_json_schema=False)]


async def open_mcp(stack: AsyncExitStack, space, still_running, log) -> tuple[list, list[str]]:
    """Connect the owner's MCP servers for one task: (tools, notes for the prompt). Close by closing the stack."""
    if not space.is_owner:
        return [], []
    tools, notes = [], []
    direct_count, direct_characters = 0, 0
    for name, entry in load()["mcp"].items():
        server = None
        is_higgsfield = entry.get("auth") == "higgsfield"
        try:
            server = _server(name, entry, space)
            async with asyncio.timeout(CONNECT_SECONDS):  # not wait_for: MCP's task groups must stay in this task
                await server.connect()
            stack.push_async_callback(server.cleanup)
            async with asyncio.timeout(60):
                listed = await server.list_tools()
        except BaseException as error:  # a broken server must not stop the task
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, asyncio.CancelledError) and asyncio.current_task().cancelling():
                raise
            try:
                if server is not None:
                    await server.cleanup()
            except BaseException:
                pass
            notes.append(f"- {name} (MCP): unavailable right now ({type(error).__name__})")
            if is_higgsfield:
                notes.append("Higgsfield sign-in may need renewal: ask the owner to use /connect higgsfield.")
            log("tool", f"MCP server {name} is unavailable ({type(error).__name__})")
            continue
        wrapped = [wrap(name, server, tool, still_running, log, higgsfield=is_higgsfield) for tool in listed]
        size = sum(len(json.dumps({"description": tool.description, "schema": tool.params_json_schema},
                                  ensure_ascii=False)) for tool in wrapped)
        advertised_names = {tool.name for tool in tools}
        unique = (len({tool.name for tool in wrapped}) == len(wrapped)
                  and not any(tool.name in advertised_names for tool in wrapped))
        if (unique and direct_count + len(wrapped) <= DIRECT_TOOL_LIMIT
                and direct_characters + size <= DIRECT_CATALOG_CHARACTERS):
            tools += wrapped
            direct_count += len(wrapped)
            direct_characters += size
            notes.append(f"- {name} (MCP): {len(wrapped)} tools, named {tool_name(name, '…')}")
        else:
            tools += discovery_tools(name, wrapped, listed, still_running)
            notes.append(f"- {name} (MCP): {len(wrapped)} tools available through {catalog_tool_name(name, 'discover_tools')} "
                         f"and {catalog_tool_name(name, 'invoke_tool')}. Search for relevant capabilities, read the exact tool's "
                         "schema_json pages, then invoke its original name with an arguments object.")
        if is_higgsfield:
            notes.append(HIGGSFIELD_GUIDE)
    return tools, notes


# ---------------------------------------------------------------------------------------------
# HTTP APIs -> one call_api tool
# ---------------------------------------------------------------------------------------------
def _redact(text: str, entry: dict) -> str:
    for secret in list((entry.get("headers") or {}).values()) + list((entry.get("query") or {}).values()):
        for piece in {secret, secret.removeprefix("Bearer ").strip()}:
            if len(piece) >= 6:
                text = text.replace(piece, "[hidden]")
    return text


async def call(name: str, method: str = "GET", path: str = "/", query: dict | None = None, body=None,
               transport=None) -> str:
    entry = load()["api"].get(name)
    if not entry:
        return f"No API named {name!r}. Connected: {', '.join(load()['api']) or 'none'}."
    method = method.upper()
    if method not in entry["methods"]:
        return f"{method} isn't allowed for {name} (allowed: {', '.join(entry['methods'])})."
    if (not path.startswith("/") or path.startswith("//") or "://" in path or "\\" in path
            or ".." in path.split("?")[0].split("/")):
        return "path must be a relative path starting with /, e.g. /v1/items?limit=5"
    base = urlsplit(entry["base_url"])
    target = urlsplit(entry["base_url"] + path)
    if target.netloc != base.netloc:
        return "That path would leave the API's host."
    url = target._replace(query="", fragment="").geturl()
    # The path's own query, then the model's query_json, then the stored keys (which always win).
    params = [(k, v) for k, v in parse_qsl(target.query, keep_blank_values=True)
              if k not in (entry.get("query") or {})]
    params += [(str(k), str(v)) for k, v in (query or {}).items() if str(k) not in (entry.get("query") or {})]
    params += list((entry.get("query") or {}).items())
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, transport=transport) as client:
        try:
            response = await client.request(method, url, params=params or None, headers=entry.get("headers") or {},
                                            json=body if body is not None and method != "GET" else None)
        except httpx.HTTPError as error:
            return _redact(f"{name} couldn't be reached: {type(error).__name__}", entry)
    kind = response.headers.get("content-type", "").split(";")[0]
    if 300 <= response.status_code < 400:
        text = f"Redirect to {response.headers.get('location', '?')} (not followed)."
    elif "json" in kind:
        try:
            text = json.dumps(response.json(), ensure_ascii=False, indent=1)
        except ValueError:
            text = response.text
    elif kind.startswith("text/") or kind in {"application/xml", ""}:
        text = response.text
    else:
        text = f"[{kind} body of {len(response.content):,} bytes omitted]"
    text = text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n[… {len(text) - MAX_OUTPUT} more characters cut]"
    return _redact(f"HTTP {response.status_code} {kind}\n{text}", entry)


def api_tool(still_running, log):
    """The call_api tool, or None when no API is connected."""
    apis = load()["api"]
    if not apis:
        return None, []
    from agents import function_tool

    @function_tool
    async def call_api(api: str, method: str = "GET", path: str = "/", query_json: str = "", body_json: str = "") -> str:
        """Call one of the owner's connected HTTP APIs (listed under CONNECTED TOOLS in your instructions). The stored
        keys and headers are added for you: never ask for or include them. path is relative to the API's base URL,
        e.g. /v1/items. query_json and body_json are optional JSON objects. Returns the status and the response body."""
        still_running()
        try:
            query = json.loads(query_json) if query_json.strip() else None
            body = json.loads(body_json) if body_json.strip() else None
        except ValueError:
            return "query_json and body_json must be JSON."
        log("tool", f"API {api}: {method.upper()} {path[:120]}")
        return await call(api, method, path, query if isinstance(query, dict) else None, body)

    notes = [f"- {name} (API, call_api): {entry['base_url']}, methods {'/'.join(entry['methods'])}"
             + (f" — {entry['about']}" if entry.get("about") else "") for name, entry in apis.items()]
    return call_api, notes


async def check_mcp(name: str, space) -> dict:
    """Connect once and list the server's tools (used right after /connect mcp)."""
    entry = load()["mcp"].get(name)
    if not entry:
        raise ValueError("No such MCP server")
    async with AsyncExitStack() as stack:
        tools, notes = await open_mcp(stack, space, lambda: None, lambda kind, detail: None)
    return {"name": name, "tools": [t.name for t in tools], "note": notes[0] if notes else ""}
