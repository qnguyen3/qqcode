from __future__ import annotations

import ast
from collections.abc import Iterator
import io
import json
import math
import re
import socket
import statistics
import sys
from typing import Any, NoReturn, SupportsIndex, TextIO

try:
    import resource
except ImportError:
    resource = None


_RIPGREP_PARTS = 3


class _SandboxAbort(BaseException):
    """Un-catchable by user ``except Exception:`` blocks; halts script execution."""


class _MatchLine(str):
    """Single line of grep/find/ls output supporting both ``str`` and dict/attr access."""

    def _parsed(self) -> tuple[str, int, str]:
        text = str(self)
        parts = text.split(":", 2)
        if len(parts) == _RIPGREP_PARTS and parts[1].isdigit():
            return parts[0], int(parts[1]), parts[2]
        return text, 0, text

    def __getitem__(self, key: SupportsIndex | slice | str) -> Any:
        match key:
            case "path":
                return self._parsed()[0]
            case "line":
                return self._parsed()[1]
            case "content" | "text":
                return self._parsed()[2]
            case str():
                raise KeyError(key)
            case _:
                return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        match key:
            case "path" | "line" | "content" | "text":
                return self[key]
            case _:
                return default

    @property
    def path(self) -> str:
        return self._parsed()[0]

    @property
    def line(self) -> int:
        return self._parsed()[1]

    @property
    def content(self) -> str:
        return self._parsed()[2]

    @property
    def text(self) -> str:
        return self._parsed()[2]


class _MatchText(str):
    """Multi-line tool output string whose iteration yields :class:`_MatchLine` rows."""

    def __iter__(self) -> Iterator[_MatchLine]:
        for raw_line in super().splitlines():
            if raw_line:
                yield _MatchLine(raw_line)

    def splitlines(self, keepends: bool = False) -> list[str]:
        return [_MatchLine(line) for line in super().splitlines(keepends)]


class _AttrDict(dict[str, Any]):
    """Dictionary with attribute access for tool result fields."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(
                f"Tool result has no attribute {name!r}. Available keys: {list(self.keys())}"
            ) from exc


def _wrap_value(value: Any, *, key: str | None = None) -> Any:
    match value:
        case dict():
            return _AttrDict({
                str(k): _wrap_value(v, key=str(k)) for k, v in value.items()
            })
        case list():
            return [_wrap_value(item) for item in value]
        case str() if key in {"matches", "entries"}:
            return _MatchText(value)
        case _:
            return value


class _SafeModule:
    """Read-only proxy exposing only allowlisted attributes from a stdlib module."""

    def __init__(self, target: Any, allowed: list[str] | tuple[str, ...]) -> None:
        self._attrs: dict[str, Any] = {
            name: getattr(target, name)
            for name in allowed
            if not name.startswith("_") and hasattr(target, name)
        }

    def __getattr__(self, name: str) -> Any:
        if name in self._attrs:
            return self._attrs[name]
        raise AttributeError(f"Module attribute {name!r} is not available in sandbox")


class _OutputCapture(io.TextIOBase):
    """Captures script stdout, streams chunks to parent, and caps at max_output_bytes."""

    def __init__(self, rpc_out: TextIO, max_output_bytes: int) -> None:
        self._rpc_out = rpc_out
        self._max_bytes = max(1, max_output_bytes)
        self._chunks: list[str] = []
        self._bytes_used = 0
        self.truncated = False

    def write(self, s: str) -> int:
        if not s:
            return 0

        encoded = s.encode("utf-8", errors="replace")
        remaining = self._max_bytes - self._bytes_used
        if len(encoded) <= remaining:
            self._chunks.append(s)
            self._bytes_used += len(encoded)
            self._send_chunk(s)
            return len(s)

        if remaining > 0:
            clipped = encoded[:remaining].decode("utf-8", errors="ignore")
            if clipped:
                self._chunks.append(clipped)
                self._bytes_used += len(clipped.encode("utf-8"))
                self._send_chunk(clipped)

        self.truncated = True
        raise _SandboxAbort(
            f"Script output exceeded max_output_bytes ({self._max_bytes})"
        )

    def flush(self) -> None:
        return None

    def script_print(
        self,
        *values: Any,
        sep: str = " ",
        end: str = "\n",
        file: Any = None,
        flush: bool = False,
    ) -> None:
        del file, flush
        text = sep.join(str(v) for v in values) + end
        self.write(text)

    def text(self) -> str:
        return "".join(self._chunks)

    def _send_chunk(self, chunk: str) -> None:
        msg = json.dumps({"type": "output", "data": chunk})
        self._rpc_out.write(f"{msg}\n")
        self._rpc_out.flush()


class _ToolCallable:
    def __init__(self, name: str, proxy: _ToolsProxy) -> None:
        self._name = name
        self._proxy = proxy

    def __call__(self, *positional: Any, **kwargs: Any) -> Any:
        return self._proxy.invoke(self._name, positional, kwargs)


class _ToolsProxy:
    """Exposes ``tools.<name>(**args)`` inside the sandbox via JSON-RPC over stdio."""

    def __init__(self, rpc_in: TextIO, rpc_out: TextIO) -> None:
        self._rpc_in = rpc_in
        self._rpc_out = rpc_out

    def __getattr__(self, name: str) -> _ToolCallable:
        if name.startswith("_"):
            raise AttributeError(name)
        return _ToolCallable(name, self)

    def __call__(self, name: str, *positional: Any, **kwargs: Any) -> Any:
        return self.invoke(name, positional, kwargs)

    def invoke(
        self, name: str, positional: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        request = json.dumps({
            "type": "call",
            "tool": name,
            "args": kwargs,
            "positional": list(positional),
        })
        self._rpc_out.write(f"{request}\n")
        self._rpc_out.flush()

        line = self._rpc_in.readline()
        if not line:
            raise _SandboxAbort("Tool bridge closed connection")

        response = json.loads(line)
        if response.get("abort"):
            raise _SandboxAbort(
                str(response.get("error") or "Script execution aborted")
            )
        if not response.get("ok", False):
            raise RuntimeError(str(response.get("error") or f"Tool '{name}' failed"))
        return _wrap_value(response.get("result", {}))


def _apply_memory_limit(memory_mb: int) -> None:
    if memory_mb <= 0 or resource is None:
        return
    limit_bytes = int(memory_mb) * 1024 * 1024
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        target_hard = (
            limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
        )
        target_soft = min(limit_bytes, target_hard)
        resource.setrlimit(resource.RLIMIT_AS, (target_soft, target_hard))
    except (ValueError, OSError):
        pass


def _blocked_network(*_args: Any, **_kwargs: Any) -> NoReturn:
    raise RuntimeError("Network access is disabled in tool scripts")


def _disable_network() -> None:
    for attr in (
        "socket",
        "create_connection",
        "create_server",
        "socketpair",
        "fromfd",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    ):
        if hasattr(socket, attr):
            setattr(socket, attr, _blocked_network)


def _validate_ast(code: str) -> ast.Module:
    tree = ast.parse(code, filename="<script>", mode="exec")
    for node in ast.walk(tree):
        match node:
            case ast.Attribute(attr=attr) if attr.startswith("__"):
                raise RuntimeError(
                    f"Access to dunder attribute '{attr}' is disabled in tool scripts"
                )
            case ast.Name(id=ident) if ident.startswith("__") and ident != "__name__":
                if ident == "__import__":
                    raise ImportError(
                        "__import__ is disabled in tool scripts; only preloaded modules are allowed"
                    )
                raise RuntimeError(
                    f"Access to dunder identifier '{ident}' is disabled in tool scripts"
                )
            case _:
                pass
    return tree


def _build_restricted_globals(
    capture: _OutputCapture, tools_proxy: _ToolsProxy
) -> dict[str, Any]:
    safe_builtins: dict[str, Any] = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "chr": chr,
        "dict": dict,
        "divmod": divmod,
        "enumerate": enumerate,
        "Exception": Exception,
        "ValueError": ValueError,
        "RuntimeError": RuntimeError,
        "KeyError": KeyError,
        "IndexError": IndexError,
        "TypeError": TypeError,
        "ZeroDivisionError": ZeroDivisionError,
        "StopIteration": StopIteration,
        "filter": filter,
        "float": float,
        "frozenset": frozenset,
        "int": int,
        "isinstance": isinstance,
        "iter": iter,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "next": next,
        "ord": ord,
        "pow": pow,
        "print": capture.script_print,
        "range": range,
        "repr": repr,
        "reversed": reversed,
        "round": round,
        "set": set,
        "slice": slice,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "True": True,
        "False": False,
        "None": None,
    }
    return {
        "__builtins__": safe_builtins,
        "__name__": "__script__",
        "tools": tools_proxy,
        "json": _SafeModule(json, ("dumps", "loads", "JSONDecodeError")),
        "re": _SafeModule(
            re, [n for n in getattr(re, "__all__", dir(re)) if not n.startswith("_")]
        ),
        "math": _SafeModule(math, [n for n in dir(math) if not n.startswith("_")]),
        "statistics": _SafeModule(
            statistics,
            [
                n
                for n in getattr(statistics, "__all__", dir(statistics))
                if not n.startswith("_")
            ],
        ),
    }


def main() -> int:
    rpc_in = sys.stdin
    rpc_out = sys.stdout

    init_line = rpc_in.readline()
    if not init_line:
        return 1

    payload = json.loads(init_line)
    code = str(payload.get("code", ""))
    max_output_bytes = int(payload.get("max_output_bytes", 16384))
    memory_mb = int(payload.get("memory_mb", 256))

    _disable_network()
    _apply_memory_limit(memory_mb)

    capture = _OutputCapture(rpc_out, max_output_bytes)
    tools_proxy = _ToolsProxy(rpc_in, rpc_out)
    restricted_globals = _build_restricted_globals(capture, tools_proxy)

    sys.stdin = io.StringIO("")
    sys.stdout = capture

    error_msg: str | None = None
    try:
        tree = _validate_ast(code)
        compiled = compile(tree, filename="<script>", mode="exec")
        exec(compiled, restricted_globals, restricted_globals)
    except _SandboxAbort as exc:
        error_msg = str(exc)
    except MemoryError:
        error_msg = "MemoryError: Script exceeded memory limit"
    except BaseException as exc:
        error_msg = f"{type(exc).__name__}: {exc}"

    done_msg = json.dumps({
        "type": "done",
        "output": capture.text(),
        "truncated": capture.truncated,
        "error": error_msg,
    })
    rpc_out.write(f"{done_msg}\n")
    rpc_out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
